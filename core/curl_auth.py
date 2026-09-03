"""Parse browser-copied Plaud cURL and commit its credential tuple safely."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

REQUIRED_HEADERS = {
    "authorization": "PLAUD_AUTHORIZATION",
    "x-device-id": "PLAUD_X_DEVICE_ID",
}

OPTIONAL_HEADERS = {
    "x-pld-user": "PLAUD_X_PLD_USER",
    "x-pld-tag": "PLAUD_X_PLD_TAG",
    "app-language": "PLAUD_APP_LANGUAGE",
    "app-platform": "PLAUD_APP_PLATFORM",
    "edit-from": "PLAUD_EDIT_FROM",
    "origin": "PLAUD_ORIGIN",
    "referer": "PLAUD_REFERER",
    "timezone": "PLAUD_TIMEZONE",
}

DEFAULTS = {
    "PLAUD_BASE_URL": "https://api-apne1.plaud.ai",
    "PLAUD_APP_LANGUAGE": "en",
    "PLAUD_APP_PLATFORM": "web",
    "PLAUD_EDIT_FROM": "web",
    "PLAUD_ORIGIN": "https://web.plaud.ai",
    "PLAUD_REFERER": "https://web.plaud.ai/",
    "PLAUD_TIMEZONE": "Asia/Seoul",
}

# A normal browser "Copy as cURL" payload is only a few kilobytes. Keep this
# aligned with the Windows loopback UI so an accidental clipboard dump cannot
# make ``shlex.split`` consume unbounded memory or CPU.
MAX_CURL_BYTES = 256 * 1024
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")


def _reject_control_characters(value: str) -> None:
    """Reject C0/DEL in any value that may enter the credential store."""

    if _CONTROL_CHARACTER_RE.search(value):
        # Never reflect the offending header/cookie value: it may contain the
        # very secret we are refusing to persist.
        raise ValueError("copied cURL contains a control character in a credential value")


def parse_curl(curl: str) -> dict[str, str]:
    """Return validated values from Chrome bash or Windows-cmd cURL."""

    # Reject obviously oversized Python strings before allocating a second,
    # potentially huge UTF-8 buffer. The encoded check enforces the actual
    # cross-platform byte limit for non-ASCII clipboard contents.
    if len(curl) > MAX_CURL_BYTES or len(curl.encode("utf-8")) > MAX_CURL_BYTES:
        raise ValueError("copied cURL is unexpectedly large; copy one Plaud Network request")
    if "\x00" in curl:
        raise ValueError("copied cURL contains an invalid NUL character")

    out: dict[str, str] = dict(DEFAULTS)
    normalized = curl.replace("\\\r\n", " ").replace("\\\n", " ")
    normalized = normalized.replace("^\r\n", " ").replace("^\n", " ")
    normalized = re.sub(r'\^(["&|<>^])', r"\1", normalized)
    try:
        tokens = shlex.split(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid cURL quoting: {exc}") from None
    executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower() if tokens else ""
    if executable not in ("curl", "curl.exe"):
        raise ValueError("input is not a cURL command copied from Plaud Web")

    request_url: str | None = None

    def capture_header(raw: str) -> None:
        _reject_control_characters(raw)
        if ":" not in raw:
            return
        key, _, val = raw.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "cookie":
            out["PLAUD_COOKIE"] = val
        elif key in REQUIRED_HEADERS:
            out[REQUIRED_HEADERS[key]] = val
        elif key in OPTIONAL_HEADERS:
            out[OPTIONAL_HEADERS[key]] = val

    index = 0
    while index < len(tokens):
        token = tokens[index]
        value: str | None = None
        if token in ("-H", "--header", "-b", "--cookie", "--url"):
            if index + 1 < len(tokens):
                index += 1
                value = tokens[index]
        elif token.startswith("--header="):
            value = token.partition("=")[2]
            token = "--header"
        elif token.startswith("--cookie="):
            value = token.partition("=")[2]
            token = "--cookie"
        elif token.startswith("--url="):
            value = token.partition("=")[2]
            token = "--url"
        elif token.startswith("-H") and len(token) > 2:
            value = token[2:]
            token = "-H"
        elif token.startswith("-b") and len(token) > 2:
            value = token[2:]
            token = "-b"

        if value is not None:
            if token in ("-H", "--header"):
                capture_header(value)
            elif token in ("-b", "--cookie"):
                _reject_control_characters(value)
                cookie = value.partition(":")[2] if value.lower().startswith("cookie:") else value
                cookie = cookie.strip()
                if cookie:
                    out["PLAUD_COOKIE"] = cookie
            elif token == "--url":
                request_url = value
        elif request_url is None and token.startswith(("https://", "http://")):
            request_url = token
        index += 1

    _reject_control_characters(request_url or "")
    parsed_url = urlsplit(request_url or "")
    try:
        host = (parsed_url.hostname or "").lower()
        port = parsed_url.port
    except ValueError:
        raise ValueError("cURL contains an invalid Plaud API URL") from None
    is_plaud_api = host == "api.plaud.ai" or (
        host.startswith("api-") and host.endswith(".plaud.ai")
    )
    if (
        parsed_url.scheme != "https"
        or not is_plaud_api
        or parsed_url.username is not None
        or parsed_url.password is not None
        or port not in (None, 443)
    ):
        raise ValueError("cURL must target an https://api-*.plaud.ai request")
    out["PLAUD_BASE_URL"] = f"{parsed_url.scheme}://{parsed_url.netloc}"

    missing = [value for value in REQUIRED_HEADERS.values() if not out.get(value, "").strip()]
    if missing:
        raise ValueError(f"missing required headers in cURL: {', '.join(missing)}")
    authorization = out["PLAUD_AUTHORIZATION"].strip()
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip():
        raise ValueError("authorization header must contain a non-empty Bearer token")
    return out


def store_credentials(values: Mapping[str, str], env_path: Path) -> None:
    """Commit the complete tuple under the global rotation lock."""

    from .secret_store import credential_lock, update_credential_values
    from .ws_refresh import credential_env_updates

    with credential_lock(env_path):
        updates = credential_env_updates(dict(values), env_path, already_locked=True)
        update_credential_values(updates, env_path, already_locked=True)
