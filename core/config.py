"""Load Plaud credentials from macOS Keychain + non-secret .env settings."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

_SOURCE_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = Path(os.environ.get("PLAUD_RESOURCE_ROOT", _SOURCE_ROOT)).expanduser().resolve()
DEFAULT_ENV = Path(os.environ.get("PLAUD_ENV_FILE", PROJECT_ROOT / ".env")).expanduser()


def resolve_env_path(explicit: Path | None = None) -> Path:
    """Resolve the .env path: explicit argument > PLAUD_ENV_FILE > project default."""
    return explicit or Path(os.environ.get("PLAUD_ENV_FILE", DEFAULT_ENV))


def env_quote(value: str) -> str:
    """Single-quote a .env value, escaping backslashes and embedded quotes.

    CR/LF are stripped rather than escaped: the file format is line-based, so
    an embedded newline (e.g. smuggled through a captured JSON field) would
    otherwise split the entry and corrupt every key after it. No legitimate
    header, token, or id contains one.
    """
    cleaned = value.replace("\r", "").replace("\n", "")
    escaped = cleaned.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _env_unquote(raw: str) -> str:
    """Reverse `env_quote`, tolerating hand-edited unquoted/double-quoted values."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        quote = raw[0]
        inner = raw[1:-1]
        out: list[str] = []
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch == "\\" and i + 1 < len(inner) and inner[i + 1] in ("\\", quote):
                out.append(inner[i + 1])
                i += 2
            else:
                out.append(ch)
                i += 1
        return "".join(out)
    return raw


def read_env_file(env_path: Path) -> dict[str, str]:
    """Parse `.env` into an insertion-ordered dict (missing file -> empty).

    Comments and blank lines are skipped; rewriting via `update_env_file`
    therefore drops them — acceptable because this file is machine-managed by
    the onboard/refresh writers.
    """
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if key:
            values[key] = _env_unquote(raw)
    return values


def write_env_file(values: Mapping[str, str], env_path: Path) -> None:
    """Write the machine-managed `.env` with 0600 permissions.

    Plaud credentials now live in macOS Keychain; this file retains non-secret
    preferences and is also the legacy/test backend.  The write remains atomic
    so migration and preference updates cannot leave a torn file.
    """
    content = "\n".join(f"{key}={env_quote(value)}" for key, value in values.items()) + "\n"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = env_path.with_name(f"{env_path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, env_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    # os.replace carries the temp file's 0600 over, but tighten explicitly in
    # case a pre-existing umask/ACL loosened it.
    os.chmod(env_path, 0o600)


def update_env_file(updates: Mapping[str, str | None], env_path: Path) -> None:
    """Merge `updates` into `.env`, preserving every key not mentioned.

    A `None` value deletes the key. This is the writer credential *refreshers*
    must use: unlike `write_env_file` it cannot destroy unrelated keys (e.g. a
    token rotation must not erase PLAUD_X_DEVICE_ID).
    """
    values = read_env_file(env_path)
    for key, value in updates.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    write_env_file(values, env_path)


class ConfigError(RuntimeError):
    """Raised when Plaud credentials are missing/unconfigured."""


class PlaudConfig(BaseModel):
    base_url: str = "https://api-apne1.plaud.ai"
    authorization: str
    x_device_id: str
    # Legacy: current Plaud Web requests omit x-pld-user. Keep sending an old
    # captured value when present, but never require or synthesize one.
    x_pld_user: str = ""
    x_pld_tag: str = ""  # legacy header; current web API no longer sends it
    app_language: str = "en"
    app_platform: str = "web"
    edit_from: str = "web"
    origin: str = "https://web.plaud.ai"
    referer: str = "https://web.plaud.ai/"
    timezone: str = "Asia/Seoul"
    cookie: str = ""

    def headers(self) -> dict[str, str]:
        h = {
            "accept": "application/json, text/plain, */*",
            "app-language": self.app_language,
            "app-platform": self.app_platform,
            "authorization": self.authorization,
            "edit-from": self.edit_from,
            "origin": self.origin,
            "referer": self.referer,
            "timezone": self.timezone,
            "x-device-id": self.x_device_id,
        }
        if self.x_pld_user:
            h["x-pld-user"] = self.x_pld_user
        if self.x_pld_tag:  # only send the legacy tag header when present
            h["x-pld-tag"] = self.x_pld_tag
        if self.cookie:
            h["cookie"] = self.cookie
        return h


def _maybe_auto_refresh(env_path: Path) -> None:
    """Best-effort headless token renewal before credentials are read.

    Every CLI entry point funnels through `load_config`, so hooking here makes
    the whole surface self-healing: once `ws-bootstrap` (or one embedded web
    login) has stored a workspace refresh token, an expiring 24h token is
    renewed transparently — no browser, no user action. Failures are swallowed:
    the worst case is the exact behavior we had before this hook existed.
    Opt out with PLAUD_AUTO_REFRESH=0 (tests do).
    """
    if os.environ.get("PLAUD_AUTO_REFRESH", "1") == "0":
        return
    try:
        from .ws_refresh import ensure_fresh_token

        ensure_fresh_token(env_path=env_path)
    except Exception:
        pass


def load_config(env_file: Path | None = None) -> PlaudConfig:
    env_path = resolve_env_path(env_file)
    _maybe_auto_refresh(env_path)

    # Migrate legacy plaintext credentials before python-dotenv can copy them
    # into the process environment.  Keychain is authoritative once present.
    from .secret_store import CredentialStoreError, KEYCHAIN_OWNED_KEYS, load_credential_values

    try:
        credential_values = load_credential_values(env_path)
    except CredentialStoreError as exc:
        raise ConfigError(f"Plaud credentials unavailable: {exc}") from exc

    file_values: dict[str, str] | None = None
    if env_path.exists():
        # At this point plaintext auth has already been scrubbed; loading the
        # remaining non-secret settings is safe.
        load_dotenv(env_path, override=True)
        file_values = read_env_file(env_path)

    keychain_owned = frozenset(KEYCHAIN_OWNED_KEYS)

    def value(key: str, default: str = "") -> str:
        if key in keychain_owned:
            return credential_values.get(key, default)
        # The settings file is authoritative even for deletion: load_dotenv
        # cannot remove a key left in os.environ by an earlier read.
        if file_values is not None:
            return file_values.get(key, default)
        return os.environ.get(key, default)

    required = {
        "authorization": "PLAUD_AUTHORIZATION",
        "x_device_id": "PLAUD_X_DEVICE_ID",
    }
    missing = [name for name in required.values() if not value(name)]
    if missing:
        raise ConfigError(f"missing Plaud credentials: {', '.join(missing)} (env file: {env_path})")

    from .ws_refresh import _normalize_domain

    try:
        base_url = _normalize_domain(value("PLAUD_BASE_URL", "https://api-apne1.plaud.ai"))
    except ValueError as exc:
        raise ConfigError("stored Plaud API domain is not trusted") from exc

    return PlaudConfig(
        base_url=base_url,
        authorization=value("PLAUD_AUTHORIZATION"),
        x_device_id=value("PLAUD_X_DEVICE_ID"),
        x_pld_tag=value("PLAUD_X_PLD_TAG"),
        x_pld_user=value("PLAUD_X_PLD_USER"),
        app_language=value("PLAUD_APP_LANGUAGE", "en"),
        app_platform=value("PLAUD_APP_PLATFORM", "web"),
        edit_from=value("PLAUD_EDIT_FROM", "web"),
        origin=value("PLAUD_ORIGIN", "https://web.plaud.ai"),
        referer=value("PLAUD_REFERER", "https://web.plaud.ai/"),
        timezone=value("PLAUD_TIMEZONE", "Asia/Seoul"),
        cookie=value("PLAUD_COOKIE"),
    )
