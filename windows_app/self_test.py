"""Offline smoke test for a packaged Windows Community Lite executable."""

from __future__ import annotations

import http.client
import json
import os
import secrets
import sys
import tempfile
import threading
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .launcher import runtime_imports_are_isolated
from .server import SESSION_HEADER, create_server, launch_url


class _SelfTestService:
    """Minimal service that cannot inspect credentials, storage, or the cloud."""

    @staticmethod
    def status() -> dict[str, Any]:
        return {"edition": "community-windows-lite", "self_test": True}


def _verify_dpapi_round_trip(
    *,
    native_replace: Callable[[str], None],
    native_read: Callable[[], str | None],
    native_delete: Callable[[], None],
    blob_path: Path,
) -> None:
    """Exercise the same private native primitives used by the credential store."""

    prefix = "PLAUD-DPAPI-SELF-TEST-"
    payload = (prefix + secrets.token_hex(16 * 1024))[: 16 * 1024]
    plaintext = payload.encode("utf-8")
    if len(plaintext) != 16 * 1024:
        raise RuntimeError("synthetic payload size failed")

    native_delete()
    try:
        native_replace(payload)
        encrypted = blob_path.read_bytes()
        # Checking a substantial prefix catches a backend that wrapped or
        # prefixed plaintext while avoiding any dependence on DPAPI blob size.
        if plaintext == encrypted or plaintext in encrypted or plaintext[:256] in encrypted:
            raise RuntimeError("credential blob contains plaintext")
        if native_read() != payload:
            raise RuntimeError("DPAPI round-trip failed")
    finally:
        native_delete()

    if blob_path.exists() or native_read() is not None:
        raise RuntimeError("DPAPI delete failed")


def _restore_environment(
    environ: MutableMapping[str, str],
    saved: dict[str, str | None],
) -> None:
    for key, value in saved.items():
        if value is None:
            environ.pop(key, None)
        else:
            environ[key] = value


def _run_windows_dpapi_self_test() -> bool:
    """Return whether a native Windows DPAPI check ran successfully."""

    if sys.platform != "win32":
        return False

    keys = ("LOCALAPPDATA", "PLAUD_AUTH_BLOB_FILE")
    saved = {key: os.environ.get(key) for key in keys}
    try:
        with tempfile.TemporaryDirectory(prefix="plaud-community-lite-self-test-") as root:
            local_app_data = Path(root)
            blob_path = local_app_data / "CMDSPACE" / "SelfTest" / "auth.bin"
            os.environ["LOCALAPPDATA"] = str(local_app_data)
            os.environ["PLAUD_AUTH_BLOB_FILE"] = str(blob_path)

            # Imported after launcher isolation and after redirecting this
            # check to a temporary LocalAppData tree. No real account blob is
            # ever read, replaced, or deleted.
            from core.secret_store import _native_delete, _native_read, _native_replace

            _verify_dpapi_round_trip(
                native_replace=_native_replace,
                native_read=_native_read,
                native_delete=_native_delete,
                blob_path=blob_path,
            )
    finally:
        _restore_environment(os.environ, saved)
    return True


def _request(
    server: Any,
    path: str,
    *,
    token: str | None = None,
    method: str = "GET",
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        server.server_address[1],
        timeout=3,
    )
    headers = {SESSION_HEADER: token} if token is not None else {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    body = response.read()
    response_headers = dict(response.getheaders())
    connection.close()
    return response.status, response_headers, body


def run_self_test() -> int:
    """Verify loopback security and, on Windows, a real DPAPI round-trip."""

    server = create_server(_SelfTestService())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    dpapi_checked = False
    try:
        if not runtime_imports_are_isolated():
            raise RuntimeError("Python import isolation failed")
        parsed = urlsplit(launch_url(server))
        fragment_prefix = "session="
        if (
            server.server_address[0] != "127.0.0.1"
            or parsed.hostname != "127.0.0.1"
            or parsed.query
            or not parsed.fragment.startswith(fragment_prefix)
        ):
            raise RuntimeError("loopback boundary failed")
        token = parsed.fragment.removeprefix(fragment_prefix)
        if not token or token != server.session_token:
            raise RuntimeError("session fragment failed")

        unauthenticated, _, _ = _request(server, "/api/status")
        authenticated, api_headers, api_body = _request(
            server,
            "/api/status",
            token=token,
        )
        heartbeat_status, _, heartbeat_body = _request(
            server,
            "/api/heartbeat",
            token=token,
            method="POST",
            body=b"{}",
        )
        static_status, static_headers, static_body = _request(server, "/")
        status_payload = json.loads(api_body)
        if unauthenticated != 401 or authenticated != 200:
            raise RuntimeError("API authentication failed")
        if heartbeat_status != 200 or json.loads(heartbeat_body).get("status") != "alive":
            raise RuntimeError("browser lease heartbeat failed")
        if status_payload.get("self_test") is not True:
            raise RuntimeError("API dispatch failed")
        if static_status != 200 or b"/assets/app.js" not in static_body:
            raise RuntimeError("static resources failed")
        if not api_headers.get("Content-Security-Policy", "").startswith("default-src 'none'"):
            raise RuntimeError("API security headers failed")
        if not static_headers.get("Cache-Control", "").startswith("no-store"):
            raise RuntimeError("static cache policy failed")
        dpapi_checked = _run_windows_dpapi_self_test()
    except Exception:  # noqa: BLE001 - self-test output must remain non-sensitive
        print("Windows Community Lite self-test: FAIL")
        return 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    if dpapi_checked:
        print("Windows Community Lite self-test: PASS (HTTP + DPAPI)")
    else:
        print("Windows Community Lite self-test: PASS (HTTP); SKIP (DPAPI requires Windows)")
    return 0
