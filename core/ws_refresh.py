"""Headless refresh of the 24h Plaud *workspace* token — no browser, no app click.

Plaud web auth is two-tier OAuth (decoded from the web bundle, 2026-06-20):

    Account tier    POST {api}/auth/refresh-user-token        (httpOnly cookie)
    Workspace tier  POST {domain}/user-app/auth/workspace/refresh/{workspaceId}
                    header: Authorization: bearer {workspaceRefreshToken}
                    -> { data: { workspace_token, expires_in,
                                  refresh_token, refresh_expires_in } }

`workspace_token` IS the `client_id: web` 24h JWT the app stores in
`PLAUD_AUTHORIZATION`. While the *workspace refresh token* remains accepted we
can mint fresh 24h tokens headlessly. Plaud may revoke that rotating chain
before its advertised horizon; the macOS app then uses its persistent account
session to obtain a new workspace pair. Each normal refresh ROTATES the token,
so the new one must be persisted every cycle.

ONE-TIME BOOTSTRAP — the refresh token never appears in a copied cURL (it
lives in web.plaud.ai localStorage), so it must be captured once. Three ways:

  1. App Plaud Web Login: the capture reads Plaud's namespaced localStorage
     workspace list, so a single in-app login arms automatic refresh.
  2. `uv run plaud ws-bootstrap`: paste the workspaceList export. In the
     web.plaud.ai devtools Console:

         copy(localStorage.getItem(Object.keys(localStorage).find(k =>
           k.startsWith("pld_") && k.endsWith(":workspaceList"))))

  3. Any tool that imports PLAUD_WS_REFRESH_TOKEN / PLAUD_WORKSPACE_ID into
     the Keychain credential bundle.

Thereafter `ensure_fresh_token` (hooked into `load_config`, i.e. every CLI
command) renews the 24h token whenever less than REFRESH_WHEN_REMAINING is
left. `plaud ws-refresh` forces a renewal on demand.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .auth_status import _decode_jwt_payload, mask_id
from .config import resolve_env_path
from .secret_store import (
    CredentialStoreError,
    credential_lock,
    load_credential_values,
    update_credential_values,
)

# Refresh proactively once the access token has less than this left (seconds).
# The web app uses a similar proactive window (`Wb`) rather than waiting for 401.
REFRESH_WHEN_REMAINING = 6 * 3600
# Warn loudly once the *refresh* token itself is within this window of expiring,
# because that is the point where a manual re-bootstrap becomes necessary.
REFRESH_TOKEN_WARN_WINDOW = 3 * 24 * 3600

# Keys owned by a full credential capture (cURL import / web login). A
# fresh capture sets or explicitly clears each of these; everything else in
# bundle — the PLAUD_WS_* bootstrap keys above all — is preserved.
CAPTURE_OWNED_KEYS = (
    "PLAUD_BASE_URL",
    "PLAUD_AUTHORIZATION",
    "PLAUD_X_DEVICE_ID",
    "PLAUD_X_PLD_USER",
    "PLAUD_X_PLD_TAG",
    "PLAUD_COOKIE",
    "PLAUD_APP_LANGUAGE",
    "PLAUD_APP_PLATFORM",
    "PLAUD_EDIT_FROM",
    "PLAUD_ORIGIN",
    "PLAUD_REFERER",
    "PLAUD_TIMEZONE",
)


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Raw outcome of the workspace refresh POST (server's contract, untouched)."""

    workspace_token: str
    expires_in: int  # seconds the new access token is valid
    refresh_token: str  # ROTATED — replaces the one we sent
    refresh_expires_in: int | None  # seconds the new refresh token is valid


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    # ok | fresh | not_bootstrapped | rejected | unreachable | write_failed
    # | invalid_payload  ("fresh" = token still valid, nothing was done)
    status: str
    detail: str = ""
    access_expires_at: int | None = None  # epoch seconds
    refresh_expires_at: int | None = None  # epoch seconds
    refresh_expiring_soon: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceBootstrap:
    """One workspace entry from web.plaud.ai localStorage `workspaceList`."""

    workspace_id: str
    refresh_token: str
    refresh_expires_at: int | None  # epoch seconds
    domain: str | None


class WorkspaceRefreshAPIError(RuntimeError):
    """A non-zero Plaud business status from the refresh endpoint."""

    def __init__(self, status: object) -> None:
        self.status = status
        super().__init__(f"refresh rejected (status={status})")


def _strip_bearer(authorization: str) -> str:
    token = authorization.strip()
    for prefix in ("bearer ", "Bearer "):
        if token.startswith(prefix):
            return token[len(prefix) :].strip()
    return token


def _wid_from_authorization(authorization: str) -> str | None:
    claims = _decode_jwt_payload(_strip_bearer(authorization)) or {}
    wid = claims.get("wid")
    return str(wid) if wid else None


def _env_value(values: Mapping[str, str], key: str) -> str:
    """Stored credential first, explicit process environment as fallback."""
    return values.get(key) or os.environ.get(key) or ""


def _access_token_remaining(values: Mapping[str, str], now: int) -> int | None:
    claims = _decode_jwt_payload(_strip_bearer(_env_value(values, "PLAUD_AUTHORIZATION"))) or {}
    exp = claims.get("exp")
    return int(exp) - now if isinstance(exp, (int, float)) else None


def _normalize_epoch_seconds(value: object) -> int | None:
    """Coerce an epoch that may arrive as int/float/str, seconds or millis."""
    try:
        n = int(float(str(value)))
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n > 10**11:  # epoch millis (10**11 seconds is the year 5138)
        n //= 1000
    return n


def _normalize_domain(domain: str) -> str:
    candidate = domain.strip().rstrip("/")
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").lower()
    trusted_host = host == "api.plaud.ai" or (host.startswith("api") and host.endswith(".plaud.ai"))
    if (
        parsed.scheme.lower() != "https"
        or not trusted_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("workspace API domain must be an HTTPS api*.plaud.ai origin")
    return f"https://{host}"


def parse_workspace_list(text: str) -> list[WorkspaceBootstrap]:
    """Parse a web.plaud.ai `workspaceList` export (raw localStorage value or
    the devtools-console copy). Raises ValueError on non-JSON input; entries
    without both workspaceId and refreshToken are skipped."""
    try:
        data = json.loads(text)
        if isinstance(data, str):  # double-encoded (JSON.stringify of the raw string)
            data = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc.msg}") from exc
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("expected a JSON array of workspace entries")

    entries: list[WorkspaceBootstrap] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        wid = item.get("workspaceId") or item.get("workspace_id")
        token = item.get("refreshToken") or item.get("refresh_token")
        if not wid or not token:
            continue
        expires = item.get("refreshExpiresAt") or item.get("refresh_expires_at")
        domain = item.get("domain")
        normalized_domain = _normalize_domain(str(domain)) if domain else None
        entries.append(
            WorkspaceBootstrap(
                workspace_id=str(wid),
                refresh_token=str(token),
                refresh_expires_at=_normalize_epoch_seconds(expires),
                domain=normalized_domain,
            )
        )
    return entries


def select_workspace_entry(
    entries: list[WorkspaceBootstrap], *, wid: str | None
) -> WorkspaceBootstrap | None:
    """Pick the entry for the workspace we are authenticated against.

    With a known wid only an exact match is accepted (a refresh token for a
    different workspace would mint tokens for the wrong data set). Without one,
    a single-entry list is unambiguous; anything else is refused.
    """
    if wid:
        return next((e for e in entries if e.workspace_id == wid), None)
    return entries[0] if len(entries) == 1 else None


def credential_env_updates(
    captured: Mapping[str, str],
    env_path: Path,
    *,
    workspace_list_json: str | None = None,
    replace_workspace_refresh: bool = False,
    already_locked: bool = False,
) -> dict[str, str | None]:
    """Turn a full credential capture into a merge-safe credential update.

    Capture-owned keys are set or explicitly cleared (a stale cookie must not
    outlive the login that replaced it). The PLAUD_WS_* bootstrap keys are
    preserved so a cURL re-import cannot disarm headless refresh — unless the
    new token belongs to a *different* workspace (stale, cleared).  A captured
    localStorage token normally does not replace an existing token for the same
    workspace: the stored one may already have rotated beyond the browser's
    stale copy. ``replace_workspace_refresh`` is reserved for a newer/recovered
    WebKit generation whose access token was validated in memory.
    """
    updates: dict[str, str | None] = {key: captured.get(key) or None for key in CAPTURE_OWNED_KEYS}
    new_wid = _wid_from_authorization(captured.get("PLAUD_AUTHORIZATION", ""))
    stored = load_credential_values(env_path, already_locked=already_locked)
    stored_wid = stored.get("PLAUD_WORKSPACE_ID") or _wid_from_authorization(
        stored.get("PLAUD_AUTHORIZATION", "")
    )
    stored_refresh = stored.get("PLAUD_WS_REFRESH_TOKEN")

    entry: WorkspaceBootstrap | None = None
    if workspace_list_json:
        try:
            entry = select_workspace_entry(parse_workspace_list(workspace_list_json), wid=new_wid)
        except ValueError:
            entry = None  # malformed export is not fatal — headless refresh just stays unarmed

    if entry is not None and (
        replace_workspace_refresh or not (stored_refresh and stored_wid == entry.workspace_id)
    ):
        updates["PLAUD_WORKSPACE_ID"] = entry.workspace_id
        updates["PLAUD_WS_REFRESH_TOKEN"] = entry.refresh_token
        updates["PLAUD_WS_REFRESH_EXPIRES_AT"] = (
            str(entry.refresh_expires_at) if entry.refresh_expires_at else None
        )
        if entry.domain:
            updates["PLAUD_BASE_URL"] = _normalize_domain(entry.domain)
    elif entry is None:
        if stored_wid and new_wid and stored_wid != new_wid:
            updates["PLAUD_WORKSPACE_ID"] = None
            updates["PLAUD_WS_REFRESH_TOKEN"] = None
            updates["PLAUD_WS_REFRESH_EXPIRES_AT"] = None
    return updates


def _call_workspace_refresh(
    *, base_url: str, workspace_id: str, refresh_token: str, timeout: float = 15.0
) -> RefreshResult:
    """POST the workspace refresh and return the server's rotated credentials.

    Raises httpx errors on transport failure, RuntimeError on a non-zero
    business status (e.g. an invalid / expired refresh token).
    """
    trusted_base_url = _normalize_domain(base_url)
    url = f"{trusted_base_url}/user-app/auth/workspace/refresh/{workspace_id}"
    resp = httpx.post(
        url,
        json={},
        headers={"Authorization": f"bearer {refresh_token}"},
        timeout=timeout,
        trust_env=False,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != 0:
        raise WorkspaceRefreshAPIError(body.get("status"))
    data = body["data"]
    return RefreshResult(
        workspace_token=data["workspace_token"],
        expires_in=int(data["expires_in"]),
        refresh_token=data.get("refresh_token") or refresh_token,
        refresh_expires_in=(
            int(data["refresh_expires_in"]) if data.get("refresh_expires_in") else None
        ),
    )


def apply_refresh_result(
    result: RefreshResult, env_path: Path, *, now: int, already_locked: bool = False
) -> RefreshOutcome:
    """Persist a successful refresh atomically and report what happened.

    Refuses to write when the returned token does not decode as a JWT — a
    garbage write here would replace a merely-expiring credential with a
    broken one. The rotated refresh token is persisted in the same atomic
    write as the access token: skip it and the NEXT refresh sends a stale
    token and gets locked out.
    """
    if not already_locked:
        try:
            with credential_lock(env_path):
                return apply_refresh_result(result, env_path, now=now, already_locked=True)
        except CredentialStoreError as exc:
            return RefreshOutcome("write_failed", str(exc))

    claims = _decode_jwt_payload(result.workspace_token)
    if claims is None:
        return RefreshOutcome(
            "rejected",
            "server returned a token that does not decode as a JWT — credentials unchanged",
        )

    current = load_credential_values(env_path, already_locked=already_locked)
    expected_wid = current.get("PLAUD_WORKSPACE_ID")
    returned_wid = claims.get("wid")
    if expected_wid and returned_wid != expected_wid:
        return RefreshOutcome(
            "rejected",
            "server returned a token for a different workspace — credentials unchanged",
        )

    exp = claims.get("exp")
    access_expires_at = int(exp) if isinstance(exp, (int, float)) else now + result.expires_in

    updates: dict[str, str | None] = {
        "PLAUD_AUTHORIZATION": f"bearer {result.workspace_token}",
        "PLAUD_WS_REFRESH_TOKEN": result.refresh_token,
    }
    if result.refresh_expires_in is not None:
        refresh_expires_at: int | None = now + result.refresh_expires_in
        updates["PLAUD_WS_REFRESH_EXPIRES_AT"] = str(refresh_expires_at)
    else:
        # Server omitted it — keep the previously recorded horizon rather than
        # discarding the one signal that tells us when re-bootstrap is due.
        refresh_expires_at = _normalize_epoch_seconds(current.get("PLAUD_WS_REFRESH_EXPIRES_AT"))

    # `update_credential_values` clears any recorded server rejection: this
    # credential is new, so the old verdict no longer applies to it.
    update_credential_values(updates, env_path, already_locked=already_locked)
    return RefreshOutcome(
        "ok",
        "workspace token refreshed headlessly",
        access_expires_at=access_expires_at,
        refresh_expires_at=refresh_expires_at,
        refresh_expiring_soon=(
            refresh_expires_at is not None and refresh_expires_at - now <= REFRESH_TOKEN_WARN_WINDOW
        ),
    )


def refresh_workspace_token(
    *, env_path: Path | None = None, now: int | None = None, only_if_needed: bool = False
) -> RefreshOutcome:
    """Mint a fresh 24h token from the stored workspace refresh token.

    Runs under the global credential lock and re-reads Keychain inside it, so
    concurrent app/CLI/checkouts serialize and the second caller sees the
    rotated token. With `only_if_needed` the call is a no-op while the access
    token still has more than REFRESH_WHEN_REMAINING left.
    """
    now = int(time.time()) if now is None else now
    env_path = resolve_env_path(env_path)
    try:
        with credential_lock(env_path):
            return _refresh_workspace_token_locked(
                env_path=env_path, now=now, only_if_needed=only_if_needed
            )
    except CredentialStoreError as exc:
        return RefreshOutcome("write_failed", str(exc))


def _refresh_workspace_token_locked(
    *, env_path: Path, now: int, only_if_needed: bool = False
) -> RefreshOutcome:
    values = load_credential_values(env_path, already_locked=True)
    refresh_token = _env_value(values, "PLAUD_WS_REFRESH_TOKEN")
    if not refresh_token:
        return RefreshOutcome(
            "not_bootstrapped",
            "no workspace refresh token stored — sign in once with Plaud Web Login",
        )
    workspace_id = _env_value(values, "PLAUD_WORKSPACE_ID") or _wid_from_authorization(
        _env_value(values, "PLAUD_AUTHORIZATION")
    )
    if not workspace_id:
        return RefreshOutcome("not_bootstrapped", "workspace id unknown — sign in once again")

    remaining = _access_token_remaining(values, now)
    # `remaining` is the token's own claim. If the server has since rejected
    # it, that claim is worthless — skipping the refresh here is exactly what
    # made `auth-recover` answer "nothing to do" while every API call 419'd.
    from .auth_status import auth_rejected_at

    server_rejected = auth_rejected_at() is not None
    if (
        only_if_needed
        and not server_rejected
        and remaining is not None
        and remaining > REFRESH_WHEN_REMAINING
    ):
        return RefreshOutcome(
            "fresh",
            "access token still valid — nothing to do",
            access_expires_at=now + remaining,
            refresh_expires_at=_normalize_epoch_seconds(values.get("PLAUD_WS_REFRESH_EXPIRES_AT")),
        )

    base_url = _env_value(values, "PLAUD_BASE_URL") or "https://api-apne1.plaud.ai"
    try:
        result = _call_workspace_refresh(
            base_url=base_url, workspace_id=workspace_id, refresh_token=refresh_token
        )
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            from .auth_status import record_auth_rejection

            record_auth_rejection(status=code, now=now)
            _disarm(env_path, already_locked=True)
            return RefreshOutcome(
                "rejected",
                "refresh token invalid/expired — automatic renewal was disarmed; sign in once",
            )
        return RefreshOutcome("unreachable", f"HTTP {code}")
    except httpx.RequestError as exc:
        return RefreshOutcome("unreachable", f"network error: {exc}")
    except WorkspaceRefreshAPIError as exc:
        if exc.status in (-419, 401, 403, "-419", "401", "403"):
            from .auth_status import record_auth_rejection

            record_auth_rejection(status=exc.status, now=now)
            _disarm(env_path, already_locked=True)
            return RefreshOutcome(
                "rejected",
                "refresh token invalid/expired — automatic renewal was disarmed; sign in once",
            )
        if exc.status in (-420, 420, "-420", "420"):
            # Observed 2026-08-19: after a web.plaud.ai sign-in rotated the
            # chain, the refresh call answered -420. That is a rejection, not
            # an outage — reporting it as "unreachable" stopped the recovery
            # ladder from climbing to the browser re-harvest that fixes it.
            # The stored token is conclusively dead. Keeping it made auth look
            # "ready" and also caused Web Login to preserve it over the fresh
            # browser candidate. Disarm it; bootstrap keeps the workspace id
            # and can atomically install/verify the browser's replacement.
            from .auth_status import record_auth_rejection

            record_auth_rejection(status=exc.status, now=now)
            _disarm(env_path, already_locked=True)
            return RefreshOutcome(
                "rejected",
                "refresh token rejected (-420) — automatic renewal was disarmed; "
                "re-harvest from the browser session",
            )
        # A non-auth business failure may be transient.  Preserve the rotating
        # credential instead of destructively disarming it.
        return RefreshOutcome("unreachable", f"Plaud refresh status {exc.status}")

    try:
        return apply_refresh_result(result, env_path, now=now, already_locked=True)
    except (OSError, CredentialStoreError) as exc:
        # The server may already have rotated the token.  _write_keychain has
        # retried the same returned value; never call the network again here.
        return RefreshOutcome("write_failed", f"rotated token could not be persisted: {exc}")


def _disarm(env_path: Path, *, already_locked: bool = False) -> None:
    """Drop a dead refresh token so every subsequent CLI call doesn't pay a
    doomed network round-trip. PLAUD_WORKSPACE_ID stays — it is not a secret
    and speeds up the next bootstrap."""
    try:
        update_credential_values(
            {"PLAUD_WS_REFRESH_TOKEN": None, "PLAUD_WS_REFRESH_EXPIRES_AT": None},
            env_path,
            already_locked=already_locked,
        )
    except (OSError, CredentialStoreError):
        pass  # disarming is an optimization; failing to disarm is not an error


def ensure_fresh_token(
    *, env_path: Path | None = None, now: int | None = None
) -> RefreshOutcome | None:
    """The `load_config` hook: refresh only when needed, else touch nothing.

    The pre-check performs one atomic Keychain read plus one JWT decode. Returns
    None when no refresh was attempted.
    """
    now = int(time.time()) if now is None else now
    env_path = resolve_env_path(env_path)
    values = load_credential_values(env_path)
    if not _env_value(values, "PLAUD_WS_REFRESH_TOKEN"):
        return None
    remaining = _access_token_remaining(values, now)
    # A server-side rejection outranks the JWT's self-reported lifetime.  The
    # inner locked function already knows this, but returning here used to keep
    # it unreachable whenever a revoked token still claimed >6h remaining.
    from .auth_status import auth_rejected_at

    if auth_rejected_at() is None and remaining is not None and remaining > REFRESH_WHEN_REMAINING:
        return None
    return refresh_workspace_token(env_path=env_path, now=now, only_if_needed=True)


def bootstrap_workspace(
    text: str, *, env_path: Path | None = None, now: int | None = None
) -> RefreshOutcome:
    """One-time arm of headless refresh from a workspaceList export.

    Persists the refresh credentials, then immediately performs one real
    refresh — this both proves the pasted token works and rotates it, so the
    value sitting in the browser's localStorage is no longer the live one.
    """
    env_path = resolve_env_path(env_path)
    now = int(time.time()) if now is None else now

    try:
        entries = parse_workspace_list(text)
    except ValueError as exc:
        return RefreshOutcome("invalid_payload", str(exc))
    if not entries:
        return RefreshOutcome("invalid_payload", "no entries with workspaceId + refreshToken found")

    try:
        with credential_lock(env_path):
            values = load_credential_values(env_path, already_locked=True)
            wid = _wid_from_authorization(_env_value(values, "PLAUD_AUTHORIZATION"))
            entry = select_workspace_entry(entries, wid=wid)
            if entry is None:
                listed = ", ".join(mask_id(e.workspace_id) or "?" for e in entries)
                return RefreshOutcome(
                    "invalid_payload",
                    f"no entry matches the current workspace ({mask_id(wid) or 'unknown'}); "
                    f"export contains: {listed}",
                )

            updates: dict[str, str | None] = {
                "PLAUD_WORKSPACE_ID": entry.workspace_id,
                "PLAUD_WS_REFRESH_TOKEN": entry.refresh_token,
            }
            if entry.refresh_expires_at:
                updates["PLAUD_WS_REFRESH_EXPIRES_AT"] = str(entry.refresh_expires_at)
            if entry.domain:
                updates["PLAUD_BASE_URL"] = _normalize_domain(entry.domain)
            update_credential_values(updates, env_path, already_locked=True)
            # Validate immediately and persist only the server's rotated token.
            return _refresh_workspace_token_locked(env_path=env_path, now=now)
    except (OSError, CredentialStoreError) as exc:
        return RefreshOutcome("write_failed", f"could not persist credentials: {exc}")
