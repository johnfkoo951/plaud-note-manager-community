"""Refresh Plaud credentials from a copied Plaud cURL.

The source of truth remains the user's browser-copied cURL:

    1. Open web.plaud.ai.
    2. Copy an authenticated API request as cURL.
    3. Run `uv run plaud refresh-auth` or click the app's refresh button.

This helper reads the macOS pasteboard, parses the cURL with the same parser as
`plaud onboard`, and writes the auth bundle to macOS Keychain. Tokens/cookies
are never printed or passed in a process argument.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from cli.onboard import parse_curl, write_env

from .config import resolve_env_path
from .secret_store import CredentialStoreError


@dataclass
class RefreshResult:
    # ok | live_check_unavailable | live_auth_failed | clipboard_empty |
    # invalid_curl | pbpaste_missing | write_failed
    status: str
    detail: str = ""
    cookie_captured: bool = False


def _read_pasteboard() -> str:
    try:
        proc = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(str(exc)) from exc
    return proc.stdout


LiveValidator = Callable[[Mapping[str, str]], str]


def refresh_auth(
    *,
    env_path: Path | None = None,
    curl_text: str | None = None,
    validate_live: bool = False,
    live_validator: LiveValidator | None = None,
) -> RefreshResult:
    """Parse a copied Plaud cURL and write fresh credentials to Keychain.

    App callers request a live validation so a rejected candidate never
    replaces the last usable file. CLI/tests can keep the local-only default.
    """
    env_path = resolve_env_path(env_path)  # honor PLAUD_ENV_FILE like every reader
    if curl_text is None:
        try:
            curl_text = _read_pasteboard()
        except RuntimeError as exc:
            return RefreshResult("pbpaste_missing", f"could not read macOS pasteboard: {exc}")

    if not curl_text.strip():
        return RefreshResult(
            "clipboard_empty",
            "Copy a Plaud API request as cURL from web.plaud.ai, then retry.",
        )

    try:
        values = parse_curl(curl_text)
    except SystemExit as exc:
        return RefreshResult("invalid_curl", str(exc))

    # Reject a locally-decodable expired JWT without touching disk. Opaque
    # tokens remain eligible for the live probe.
    from .web_auth import _default_live_validator, _token_expired

    if _token_expired(values["PLAUD_AUTHORIZATION"], now=int(time.time())):
        return RefreshResult("live_auth_failed", "captured token is already expired")

    status = "ok"
    detail = "credentials refreshed from copied cURL"
    if validate_live:
        verdict = (live_validator or _default_live_validator)(values)
        if verdict == "rejected":
            return RefreshResult(
                "live_auth_failed",
                "Plaud rejected the copied credentials; Keychain unchanged",
                cookie_captured="PLAUD_COOKIE" in values,
            )
        if verdict == "unreachable":
            status = "live_check_unavailable"
            detail = "credentials saved but could not be verified — check your network connection"

    # `write_env()` is intentionally chatty for terminal onboarding, but this
    # helper is consumed by the app as JSON. Keep stdout clean so the Swift UI
    # can decode `plaud refresh-auth --json` reliably.
    try:
        with redirect_stdout(StringIO()):
            write_env(values, env_path)
    except (OSError, CredentialStoreError) as exc:
        return RefreshResult("write_failed", f"could not update credentials: {exc}")
    return RefreshResult(
        status,
        detail,
        cookie_captured="PLAUD_COOKIE" in values,
    )
