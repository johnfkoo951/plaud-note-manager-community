"""Inspect macOS Keychain Plaud credentials for auth-status monitoring.

The `authorization` header is a JWT (`bearer <header>.<payload>.<sig>`); its
payload carries `iat` (issued) and `exp` (expires) plus workspace/member/role.
We can therefore report token validity and an expiry countdown *offline*, and
optionally confirm the token is actually live (not revoked) with one cheap call.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass

from .paths import DATA_DIR
from .config import ConfigError, load_config

# Warn when the token expires within this window (seconds).
EXPIRING_SOON = 2 * 3600


def mask_id(value: str | None, *, prefix: int = 6, suffix: int = 3) -> str | None:
    """Partially mask an identifier so it stays recognizable but not exposed.

    e.g. 'ws_clQNfkQoaS' -> 'ws_clQ…oaS'. Short values that can't be masked
    without revealing most of themselves are collapsed to a fixed marker.
    """
    if not value:
        return value
    if len(value) <= prefix + suffix:
        return "…" + value[-suffix:] if len(value) > suffix else "…"
    return f"{value[:prefix]}…{value[-suffix:]}"


@dataclass
class AuthStatus:
    configured: bool
    state: str  # valid | expiring | expired | unconfigured | unknown
    workspace_id: str | None = None
    member_id: str | None = None
    role: str | None = None
    issued_at: int | None = None  # epoch seconds
    expires_at: int | None = None  # epoch seconds
    seconds_remaining: int | None = None
    remaining_human: str | None = None
    live_state: str | None = None  # ok | rejected | unreachable | None = not checked
    live_ok: bool | None = None  # derived from live_state — never set directly
    # Headless-renewal readiness: ready | expiring | expired | disabled |
    # not_bootstrapped | store_unavailable.
    # "expiring"/"expired" refer to the *refresh* token (re-bootstrap horizon),
    # not the 24h access token tracked by `state`.
    auto_refresh: str | None = None
    refresh_expires_at: int | None = None  # epoch seconds, if the server told us
    detail: str = ""

    def __post_init__(self) -> None:
        # Keep live_ok derived so it can never disagree with live_state:
        # True only on a confirmed probe, False only on a genuine rejection.
        self.live_ok = (
            True if self.live_state == "ok" else False if self.live_state == "rejected" else None
        )


def _decode_jwt_payload(token: str) -> dict | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # restore base64 padding
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return None


def _human_duration(seconds: int) -> str:
    if seconds <= 0:
        return "expired"
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


# --- Server-verdict memo -----------------------------------------------------
#
# A Plaud token's JWT `exp` is only a *claim*. The server can invalidate a
# token long before that — signing in on web.plaud.ai rotates the workspace
# chain, and every previously-issued token starts coming back as -419 while
# still claiming hours of life.
#
# Offline expiry math cannot see this, and everything downstream believed it:
# `auth --json` reported "valid", the app's self-heal saw no problem, and
# `auth-recover`'s precheck returned "fresh" and stopped before recovering.
# So we persist the server's verdict the moment an API call is rejected, and
# let it override the token's own claim until a genuinely new token arrives.

REJECTION_FILE = DATA_DIR / "auth_state.json"


def record_auth_rejection(*, status: int | str | None = None, now: int | None = None) -> None:
    """Remember that the server rejected the current credential.

    Called from the API client on any auth rejection. Best-effort: a failure
    to persist must never mask the original API error.
    """
    now = int(time.time()) if now is None else now
    try:
        REJECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
        REJECTION_FILE.write_text(
            json.dumps({"rejected_at": now, "status": status}),
            encoding="utf-8",
        )
    except OSError:
        pass


def clear_auth_rejection() -> None:
    """Forget the rejection — called whenever a fresh token is written."""
    try:
        REJECTION_FILE.unlink()
    except (OSError, FileNotFoundError):
        pass


def auth_rejected_at() -> int | None:
    """Epoch seconds of the last recorded server rejection, if any."""
    try:
        raw = json.loads(REJECTION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = raw.get("rejected_at") if isinstance(raw, dict) else None
    return int(value) if isinstance(value, (int, float)) else None


def auth_status(*, live: bool = False, now: int | None = None) -> AuthStatus:
    """Return the current Plaud credential status.

    Set `live=True` to additionally confirm the token works (one list call).
    `now` is injectable for tests.
    """
    now = int(time.time()) if now is None else now
    try:
        cfg = load_config()
    except ConfigError as exc:
        return AuthStatus(
            configured=False,
            state="unconfigured",
            detail=f"{exc}  — use the app Auth button > Authenticate with Plaud",
        )

    token = cfg.authorization
    for prefix in ("bearer ", "Bearer "):
        if token.startswith(prefix):
            token = token[len(prefix) :]
            break
    token = token.strip()

    claims = _decode_jwt_payload(token)
    detail = ""
    if claims is None:
        # Opaque token: expiry can't be read offline, but the live probe below
        # still works — treat the claims as empty instead of bailing out.
        detail = "authorization is not a decodable JWT (can't read expiry)"
        claims = {}

    exp = claims.get("exp")
    iat = claims.get("iat")
    remaining = (int(exp) - now) if isinstance(exp, (int, float)) else None
    if remaining is None:
        state = "unknown"
    elif remaining <= 0:
        state = "expired"
    elif remaining <= EXPIRING_SOON:
        state = "expiring"
    else:
        state = "valid"

    # The server's verdict outranks the token's own claim. Only honor a
    # rejection recorded *after* this token was issued — otherwise a stale
    # memo would condemn the fresh token that just replaced the bad one.
    rejected_at = auth_rejected_at()
    if rejected_at is not None and state in ("valid", "expiring", "unknown"):
        issued = int(iat) if isinstance(iat, (int, float)) else None
        if issued is None or rejected_at >= issued:
            state = "rejected"
            detail = (
                detail + "; " if detail else ""
            ) + "server rejected this token (re-run `plaud auth-recover --force`)"

    auto_refresh, refresh_expires_at = _auto_refresh_state(now)

    live_state: str | None = None
    if live and state != "expired":
        # lazy import to avoid import cost when only decoding offline
        from .client import PlaudAPIError, PlaudClient

        try:
            with PlaudClient(cfg) as client:
                client.list_files(limit=1)
            live_state = "ok"
        except PlaudAPIError as exc:
            # Plaud also reports an expired workspace token as HTTP 200 with
            # business status -419, which is just as conclusive as HTTP 401/403.
            live_state = "rejected" if exc.is_auth_rejection else "unreachable"

    return AuthStatus(
        configured=True,
        state=state,
        # Mask identifiers at the source so they stay masked across CLI / --json
        # / app popover / vault dashboard — and never leave core in raw form.
        workspace_id=mask_id(claims.get("wid")),
        member_id=mask_id(claims.get("mid")),
        role=claims.get("role"),
        issued_at=int(iat) if isinstance(iat, (int, float)) else None,
        expires_at=int(exp) if isinstance(exp, (int, float)) else None,
        seconds_remaining=remaining,
        remaining_human=_human_duration(remaining) if remaining is not None else None,
        live_state=live_state,
        auto_refresh=auto_refresh,
        refresh_expires_at=refresh_expires_at,
        detail=detail,
    )


def _auto_refresh_state(now: int) -> tuple[str, int | None]:
    """Classify automatic-renewal readiness from the Keychain credential blob."""
    import os

    if os.environ.get("PLAUD_AUTO_REFRESH", "1") == "0":
        return "disabled", None

    from .config import resolve_env_path
    from .secret_store import CredentialStoreError, load_credential_values

    try:
        values: dict[str, str | None] = load_credential_values(resolve_env_path())
    except CredentialStoreError:
        return "store_unavailable", None

    if not values.get("PLAUD_WS_REFRESH_TOKEN"):
        return "not_bootstrapped", None

    from .ws_refresh import REFRESH_TOKEN_WARN_WINDOW, _normalize_epoch_seconds

    expires_at = _normalize_epoch_seconds(values.get("PLAUD_WS_REFRESH_EXPIRES_AT"))
    if expires_at is None:
        return "ready", None  # horizon unknown — assume usable until a refresh says otherwise
    if expires_at <= now:
        return "expired", expires_at
    if expires_at - now <= REFRESH_TOKEN_WARN_WINDOW:
        return "expiring", expires_at
    return "ready", expires_at
