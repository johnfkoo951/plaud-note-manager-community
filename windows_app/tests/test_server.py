from __future__ import annotations

import http.client
import io
import json
import socket
import threading
import time
from contextlib import redirect_stderr

import pytest

from windows_app import server as server_module
from windows_app.server import SESSION_HEADER, create_server, launch_url
from windows_app.service import ServiceError


class FakeService:
    def __init__(self):
        self.calls = []

    def status(self):
        self.calls.append("status")
        return {"ok": True}

    def library(self, **kwargs):
        self.calls.append(("library", kwargs))
        return {"items": []}

    def recording(self, file_id):
        self.calls.append(("recording", file_id))
        return {"id": file_id}

    def search(self, query, **kwargs):
        self.calls.append(("search", query, kwargs))
        return {"items": []}

    def integration_settings(self):
        self.calls.append("settings")
        return {"routing": {}, "elevenlabs": {}}

    def folder_preview(self):
        self.calls.append("folder-preview-get")
        return {"items": [], "phase": "none"}

    def set_routing_settings(self, provider, backend, model_id):
        self.calls.append(("settings-routing", provider, backend, model_id))
        return {"routing": {"selected_provider": provider}}

    def set_provider_key(self, provider, api_key):
        self.calls.append(("provider-key-set", provider, api_key))
        return {"provider": provider, "configured": True}

    def delete_provider_key(self, provider):
        self.calls.append(("provider-key-delete", provider))
        return {"provider": provider, "configured": False}

    def start_folder_preview(self, **kwargs):
        self.calls.append(("folder-preview", kwargs))
        return {"state": "running"}

    def start_folder_apply(self, **kwargs):
        self.calls.append(("folder-apply", kwargs))
        return {"state": "running"}

    def start_folder_undo(self, **kwargs):
        self.calls.append(("folder-undo", kwargs))
        return {"state": "running"}

    def start_elevenlabs_transcription(self, file_id, **kwargs):
        self.calls.append(("elevenlabs-transcribe", file_id, kwargs))
        return {"state": "running"}

    def start_sync(self):
        self.calls.append("sync")
        return {"state": "running"}

    def start_backfill(self, **kwargs):
        self.calls.append(("backfill", kwargs))
        return {"state": "running"}

    def import_curl(self, value):
        self.calls.append(("import", value))
        return {"status": "connected"}

    def disconnect(self):
        self.calls.append("disconnect")
        return {"status": "disconnected"}

    def export(self, file_id, kind):
        self.calls.append(("export", file_id, kind))
        return {"status": "exported"}

    def set_usage_status(self, file_id, usage_status):
        self.calls.append(("usage-status", file_id, usage_status))
        return {"usage_status": usage_status, "tags": [], "storage": "local-only"}

    def add_tag(self, file_id, tag):
        self.calls.append(("tag-add", file_id, tag))
        return {"usage_status": "unused", "tags": [tag], "storage": "local-only"}

    def remove_tag(self, file_id, tag):
        self.calls.append(("tag-remove", file_id, tag))
        return {"usage_status": "unused", "tags": [], "storage": "local-only"}

    def prepare_shutdown(self):
        self.calls.append("shutdown")
        return {"status": "shutting_down"}


class FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class BusyAwareFakeService(FakeService):
    def __init__(self):
        super().__init__()
        self.busy = False

    def prepare_shutdown(self):
        if self.busy:
            raise ServiceError("busy", "job active", status=409)
        return super().prepare_shutdown()


@pytest.fixture
def running_server():
    service = FakeService()
    token = "test-session-token"
    server = create_server(service, session_token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, service, token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(server, method, path, *, token=None, body=None, origin=None, host=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    headers = {}
    if token is not None:
        headers[SESSION_HEADER] = token
    if origin is not None:
        headers["Origin"] = origin
    if body is not None:
        headers["Content-Type"] = "application/json"
        encoded = json.dumps(body).encode("utf-8")
    else:
        encoded = None
    if host is None:
        connection.request(method, path, body=encoded, headers=headers)
    else:
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host)
        for key, value in headers.items():
            connection.putheader(key, value)
        if encoded is not None:
            connection.putheader("Content-Length", str(len(encoded)))
        connection.endheaders(encoded)
    response = connection.getresponse()
    payload = response.read()
    response_headers = dict(response.getheaders())
    connection.close()
    return response.status, response_headers, payload


def _raw_request(server, payload):
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=3) as sock:
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


def test_session_token_is_only_in_fragment(running_server):
    server, _, token = running_server
    url = launch_url(server)
    assert f"#session={token}" in url
    assert "?session=" not in url


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/status", None),
        ("GET", "/api/library", None),
        ("GET", "/api/recording?id=rec-1", None),
        ("GET", "/api/search?q=test", None),
        ("GET", "/api/settings", None),
        ("GET", "/api/folder-preview", None),
        ("POST", "/api/sync", {}),
        ("POST", "/api/backfill", {}),
        ("POST", "/api/heartbeat", {}),
        ("POST", "/api/import-curl", {"curl": "secret"}),
        ("POST", "/api/disconnect", {}),
        (
            "POST",
            "/api/settings-routing",
            {"provider": "codex", "backend": "api", "model_id": "gpt-test"},
        ),
        (
            "POST",
            "/api/provider-key-set",
            {"provider": "codex", "api_key": "private-value"},
        ),
        ("POST", "/api/provider-key-delete", {"provider": "codex"}),
        (
            "POST",
            "/api/folder-preview",
            {"use_ai": True, "confirm_external": True},
        ),
        (
            "POST",
            "/api/folder-apply",
            {
                "file_ids": ["rec-1"],
                "plan_id": "a" * 32,
                "confirm_apply": True,
            },
        ),
        ("POST", "/api/folder-undo", {"confirm_undo": True}),
        (
            "POST",
            "/api/elevenlabs-transcribe",
            {
                "file_id": "rec-1",
                "confirm_upload": True,
                "force": False,
                "language": "ko",
                "num_speakers": 0,
            },
        ),
        ("POST", "/api/export", {"file_id": "rec-1", "kind": "summary"}),
        (
            "POST",
            "/api/usage-status",
            {"file_id": "rec-1", "usage_status": "archived"},
        ),
        ("POST", "/api/tag-add", {"file_id": "rec-1", "tag": "회의"}),
        ("POST", "/api/tag-remove", {"file_id": "rec-1", "tag": "회의"}),
        ("POST", "/api/shutdown", {}),
        ("HEAD", "/api/status", None),
        ("PUT", "/api/status", {}),
        ("DELETE", "/api/status", None),
    ],
)
def test_every_api_endpoint_requires_session(running_server, method, path, body):
    server, service, _ = running_server
    status, _, _ = _request(server, method, path, body=body)
    assert status == 401
    assert service.calls == []


def test_valid_header_dispatches_and_response_is_hardened(running_server):
    server, service, token = running_server
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        status, headers, body = _request(server, "GET", "/api/status", token=token)
    assert status == 200
    assert json.loads(body) == {"ok": True}
    assert service.calls == ["status"]
    assert headers["Cache-Control"].startswith("no-store")
    assert headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert "Access-Control-Allow-Origin" not in headers
    assert stderr.getvalue() == ""


@pytest.mark.parametrize(
    ("method", "path", "body", "expected_status", "expected_call"),
    [
        (
            "GET",
            "/api/library?limit=20&offset=5",
            None,
            200,
            ("library", {"limit": "20", "offset": "5"}),
        ),
        ("GET", "/api/recording?id=rec-1", None, 200, ("recording", "rec-1")),
        (
            "GET",
            "/api/search?q=meeting&limit=10",
            None,
            200,
            ("search", "meeting", {"limit": "10"}),
        ),
        ("GET", "/api/settings", None, 200, "settings"),
        ("GET", "/api/folder-preview", None, 200, "folder-preview-get"),
        ("POST", "/api/sync", {}, 202, "sync"),
        ("POST", "/api/backfill", {"limit": 25}, 202, ("backfill", {"limit": 25})),
        ("POST", "/api/import-curl", {"curl": "candidate"}, 200, ("import", "candidate")),
        ("POST", "/api/disconnect", {}, 200, "disconnect"),
        (
            "POST",
            "/api/settings-routing",
            {"provider": "codex", "backend": "api", "model_id": "gpt-test"},
            200,
            ("settings-routing", "codex", "api", "gpt-test"),
        ),
        (
            "POST",
            "/api/provider-key-set",
            {"provider": "codex", "api_key": "private-value"},
            200,
            ("provider-key-set", "codex", "private-value"),
        ),
        (
            "POST",
            "/api/provider-key-delete",
            {"provider": "codex"},
            200,
            ("provider-key-delete", "codex"),
        ),
        (
            "POST",
            "/api/folder-preview",
            {"use_ai": True, "confirm_external": True},
            202,
            ("folder-preview", {"use_ai": True, "confirm_external": True}),
        ),
        (
            "POST",
            "/api/folder-apply",
            {
                "file_ids": ["rec-1"],
                "plan_id": "a" * 32,
                "confirm_apply": True,
            },
            202,
            (
                "folder-apply",
                {
                    "file_ids": ["rec-1"],
                    "plan_id": "a" * 32,
                    "confirm_apply": True,
                },
            ),
        ),
        (
            "POST",
            "/api/folder-undo",
            {"confirm_undo": True},
            202,
            ("folder-undo", {"confirm_undo": True}),
        ),
        (
            "POST",
            "/api/elevenlabs-transcribe",
            {
                "file_id": "rec-1",
                "confirm_upload": True,
                "force": False,
                "language": "ko",
                "num_speakers": 0,
            },
            202,
            (
                "elevenlabs-transcribe",
                "rec-1",
                {
                    "confirm_upload": True,
                    "force": False,
                    "language": "ko",
                    "num_speakers": 0,
                },
            ),
        ),
        (
            "POST",
            "/api/export",
            {"file_id": "rec-1", "kind": "summary"},
            200,
            ("export", "rec-1", "summary"),
        ),
        (
            "POST",
            "/api/usage-status",
            {"file_id": "rec-1", "usage_status": "archived"},
            200,
            ("usage-status", "rec-1", "archived"),
        ),
        (
            "POST",
            "/api/tag-add",
            {"file_id": "rec-1", "tag": "회의"},
            200,
            ("tag-add", "rec-1", "회의"),
        ),
        (
            "POST",
            "/api/tag-remove",
            {"file_id": "rec-1", "tag": "회의"},
            200,
            ("tag-remove", "rec-1", "회의"),
        ),
    ],
)
def test_valid_session_dispatches_each_data_endpoint(
    running_server,
    method,
    path,
    body,
    expected_status,
    expected_call,
):
    server, service, token = running_server
    status, _, _ = _request(server, method, path, token=token, body=body)
    assert status == expected_status
    assert service.calls == [expected_call]


def test_rejects_dns_rebinding_and_cross_origin_requests(running_server):
    server, service, token = running_server
    status, _, _ = _request(server, "GET", "/api/status", token=token, host="attacker.example")
    assert status == 421
    status, _, _ = _request(
        server,
        "GET",
        "/api/status",
        token=token,
        origin="https://attacker.example",
    )
    assert status == 403
    assert service.calls == []


def test_static_ui_is_local_only_and_does_not_embed_session(running_server):
    server, _, token = running_server
    status, headers, body = _request(server, "GET", "/")
    text = body.decode("utf-8")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert token not in text
    assert "https://" not in text
    assert "http://" not in text
    assert 'src="/assets/app.js"' in text
    assert 'href="/assets/app.css"' in text


def test_post_endpoints_accept_valid_session_without_logging_body(running_server):
    server, service, token = running_server
    secret = "curl with a highly sensitive token"
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        status, _, body = _request(
            server,
            "POST",
            "/api/import-curl",
            token=token,
            body={"curl": secret},
        )
    assert status == 200
    assert json.loads(body) == {"status": "connected"}
    assert service.calls == [("import", secret)]
    assert secret not in stderr.getvalue()
    assert stderr.getvalue() == ""


def test_provider_key_request_is_never_logged_or_echoed(running_server):
    server, service, token = running_server
    secret = "provider-secret-that-must-not-be-logged"
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        status, _, body = _request(
            server,
            "POST",
            "/api/provider-key-set",
            token=token,
            body={"provider": "codex", "api_key": secret},
        )
    assert status == 200
    assert secret not in body.decode("utf-8")
    assert secret not in stderr.getvalue()
    assert service.calls == [("provider-key-set", "codex", secret)]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/sync", {"force": True}),
        ("/api/backfill", {"limit": 25, "force": True}),
        ("/api/import-curl", {}),
        ("/api/import-curl", {"curl": "candidate", "save": True}),
        ("/api/disconnect", {"all": True}),
        ("/api/usage-status", {"file_id": "rec-1"}),
        (
            "/api/usage-status",
            {"file_id": "rec-1", "usage_status": "archived", "cloud": True},
        ),
        ("/api/tag-add", {"file_id": "rec-1", "tag": "회의", "model": "auto"}),
        ("/api/tag-remove", {"file_id": "rec-1"}),
        ("/api/folder-preview", {"use_ai": True}),
        (
            "/api/folder-preview",
            {"use_ai": True, "confirm_external": True, "apply": True},
        ),
        ("/api/folder-apply", {"file_ids": ["rec-1"], "confirm_apply": True}),
        (
            "/api/folder-apply",
            {
                "file_ids": ["rec-1"],
                "plan_id": "a" * 32,
                "confirm_apply": True,
                "folder_id": "other",
            },
        ),
        ("/api/folder-undo", {}),
        ("/api/folder-undo", {"confirm_undo": True, "file_ids": ["rec-1"]}),
        ("/api/provider-key-set", {"provider": "codex"}),
        (
            "/api/elevenlabs-transcribe",
            {
                "file_id": "rec-1",
                "confirm_upload": True,
                "force": False,
                "language": "ko",
                "num_speakers": 0,
                "retry": True,
            },
        ),
        ("/api/export", {"file_id": "rec-1", "kind": "summary", "open": True}),
        ("/api/shutdown", {"force": True}),
    ],
)
def test_local_write_routes_reject_missing_or_extra_fields(running_server, path, body):
    server, service, token = running_server
    status, _, payload = _request(server, "POST", path, token=token, body=body)
    assert status == 400
    assert json.loads(payload)["error"] == "invalid_fields"
    assert service.calls == []


def test_shutdown_endpoint_reserves_service_then_stops_server(running_server):
    server, service, token = running_server
    status, _, body = _request(
        server,
        "POST",
        "/api/shutdown",
        token=token,
        body={},
    )
    assert status == 200
    assert json.loads(body) == {"status": "shutting_down"}
    assert service.calls == ["shutdown"]


def test_idle_server_without_heartbeat_stops_after_startup_grace():
    clock = FakeClock()
    service = BusyAwareFakeService()
    server = create_server(
        service,
        session_token="lease-token",
        clock=clock,
        startup_grace_seconds=10,
        browser_idle_timeout_seconds=5,
        monitor_interval_seconds=3600,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _request(server, "GET", "/")
        server.start_idle_monitor()
        clock.advance(9.99)
        assert server.check_idle_shutdown() is False
        assert thread.is_alive()

        clock.advance(0.01)
        assert server.check_idle_shutdown() is True
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert service.calls == ["shutdown"]
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_idle_monitor_itself_stops_the_server_after_lease_expiry():
    clock = FakeClock()
    service = BusyAwareFakeService()
    server = create_server(
        service,
        session_token="lease-token",
        clock=clock,
        startup_grace_seconds=1,
        browser_idle_timeout_seconds=1,
        monitor_interval_seconds=0.01,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        server.start_idle_monitor()
        clock.advance(1)
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert service.calls == ["shutdown"]
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_authenticated_heartbeat_renews_browser_idle_lease():
    clock = FakeClock()
    service = BusyAwareFakeService()
    token = "lease-token"
    server = create_server(
        service,
        session_token=token,
        clock=clock,
        startup_grace_seconds=10,
        browser_idle_timeout_seconds=5,
        monitor_interval_seconds=3600,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _request(server, "GET", "/")
        server.start_idle_monitor()
        clock.advance(9)
        status, _, body = _request(
            server,
            "POST",
            "/api/heartbeat",
            token=token,
            body={},
        )
        assert status == 200
        assert json.loads(body) == {"status": "alive", "idle_timeout_seconds": 5}

        clock.advance(4.99)
        assert server.check_idle_shutdown() is False
        assert service.calls == []

        clock.advance(0.01)
        assert server.check_idle_shutdown() is True
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert service.calls == ["shutdown"]
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_idle_shutdown_waits_for_active_job_then_stops():
    clock = FakeClock()
    service = BusyAwareFakeService()
    service.busy = True
    server = create_server(
        service,
        session_token="lease-token",
        clock=clock,
        startup_grace_seconds=5,
        browser_idle_timeout_seconds=5,
        monitor_interval_seconds=3600,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _request(server, "GET", "/")
        server.start_idle_monitor()
        clock.advance(5)
        assert server.check_idle_shutdown() is False
        assert thread.is_alive()
        assert service.calls == []

        service.busy = False
        assert server.check_idle_shutdown() is True
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert service.calls == ["shutdown"]
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_idle_shutdown_waits_for_authenticated_request_to_finish():
    class BlockingService(BusyAwareFakeService):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def status(self):
            self.entered.set()
            assert self.release.wait(timeout=2)
            return {"ok": True}

    clock = FakeClock()
    service = BlockingService()
    token = "lease-token"
    server = create_server(
        service,
        session_token=token,
        clock=clock,
        startup_grace_seconds=5,
        browser_idle_timeout_seconds=5,
        monitor_interval_seconds=3600,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    request_thread = threading.Thread(
        target=lambda: _request(server, "GET", "/api/status", token=token),
        daemon=True,
    )
    try:
        _request(server, "GET", "/")
        server.start_idle_monitor()
        clock.advance(5)
        request_thread.start()
        assert service.entered.wait(timeout=2)

        assert server.check_idle_shutdown() is False
        assert server_thread.is_alive()
        assert service.calls == []

        service.release.set()
        request_thread.join(timeout=2)
        assert not request_thread.is_alive()
        assert server.check_idle_shutdown() is True
        server_thread.join(timeout=2)
        assert not server_thread.is_alive()
        assert service.calls == ["shutdown"]
    finally:
        service.release.set()
        request_thread.join(timeout=2)
        if server_thread.is_alive():
            server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_explicit_shutdown_rejects_another_active_request_then_succeeds():
    class BlockingService(BusyAwareFakeService):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def status(self):
            self.entered.set()
            assert self.release.wait(timeout=2)
            return {"ok": True}

    service = BlockingService()
    token = "lease-token"
    server = create_server(service, session_token=token)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    request_thread = threading.Thread(
        target=lambda: _request(server, "GET", "/api/status", token=token),
        daemon=True,
    )
    try:
        _request(server, "GET", "/")
        request_thread.start()
        assert service.entered.wait(timeout=2)

        status, _, body = _request(
            server,
            "POST",
            "/api/shutdown",
            token=token,
            body={},
        )
        assert status == 409
        assert json.loads(body)["error"] == "busy"
        assert server_thread.is_alive()

        service.release.set()
        request_thread.join(timeout=2)
        status, _, body = _request(
            server,
            "POST",
            "/api/shutdown",
            token=token,
            body={},
        )
        assert status == 200
        assert json.loads(body) == {"status": "shutting_down"}
        server_thread.join(timeout=2)
        assert not server_thread.is_alive()
        assert service.calls == ["shutdown"]
    finally:
        service.release.set()
        request_thread.join(timeout=2)
        if server_thread.is_alive():
            server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_explicit_shutdown_stops_monitor_and_server_close_releases_socket():
    service = BusyAwareFakeService()
    token = "lease-token"
    server = create_server(
        service,
        session_token=token,
        monitor_interval_seconds=3600,
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _request(server, "GET", "/")
        server.start_idle_monitor()
        assert server.idle_monitor_running is True
        status, _, body = _request(
            server,
            "POST",
            "/api/shutdown",
            token=token,
            body={},
        )
        assert status == 200
        assert json.loads(body) == {"status": "shutting_down"}
        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert server.is_closed is True
    assert server.idle_monitor_running is False
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.2)


def test_partial_authenticated_body_times_out_and_cannot_pin_shutdown():
    service = FakeService()
    token = "lease-token"
    server = create_server(
        service,
        session_token=token,
        request_socket_timeout_seconds=0.05,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sock = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2)
    try:
        sock.sendall(
            (
                "POST /api/sync HTTP/1.1\r\n"
                f"Host: {server.expected_authority}\r\n"
                f"{SESSION_HEADER}: {token}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 2\r\n"
                "\r\n"
            ).encode()
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with server._lifecycle_lock:
                active = server._active_api_requests
            if active == 1:
                break
            time.sleep(0.005)
        assert active == 1

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with server._lifecycle_lock:
                active = server._active_api_requests
            if active == 0:
                break
            time.sleep(0.005)
        assert active == 0

        status, _, body = _request(
            server,
            "POST",
            "/api/shutdown",
            token=token,
            body={},
        )
        assert status == 200
        assert json.loads(body) == {"status": "shutting_down"}
        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        sock.close()
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_visible_ui_heartbeat_stops_when_page_is_hidden(running_server):
    server, _, _ = running_server
    status, _, source = _request(server, "GET", "/assets/app.js")
    javascript = source.decode("utf-8")
    assert status == 200
    assert 'api("/api/heartbeat"' in javascript
    assert 'document.visibilityState !== "visible"' in javascript
    assert 'document.addEventListener("visibilitychange"' in javascript
    assert 'window.addEventListener("pagehide", stopVisibleSession)' in javascript
    assert "HEARTBEAT_INTERVAL_MS = 15000" in javascript


def test_elevenlabs_unknown_retry_ui_requires_double_billing_confirmation(running_server):
    server, _, _ = running_server
    status, _, source = _request(server, "GET", "/assets/app.js")
    javascript = source.decode("utf-8")

    assert status == 200
    assert "elevenRetryOutcomeUnknown" in javascript
    assert "중복 청구" in javascript
    assert "uncertain" in javascript
    assert "force: again || uncertain" in javascript


def test_folder_undo_ui_requires_explicit_confirmation_and_exact_request(running_server):
    server, _, _ = running_server
    status, _, html = _request(server, "GET", "/")
    assert status == 200
    assert 'id="undoRouteButton"' in html.decode("utf-8")

    status, _, source = _request(server, "GET", "/assets/app.js")
    javascript = source.decode("utf-8")
    assert status == 200
    assert 'api("/api/folder-undo"' in javascript
    assert "JSON.stringify({ confirm_undo: true })" in javascript
    assert "window.confirm(" in javascript


@pytest.mark.parametrize("browser_opened", [False, True])
def test_run_server_always_closes_socket_on_startup_or_runtime_failure(
    monkeypatch,
    browser_opened,
):
    class ServerSpy:
        session_token = "lease-token"
        expected_origin = "http://127.0.0.1:54321"

        def __init__(self):
            self.monitor_started = False
            self.closed = False

        def start_idle_monitor(self):
            self.monitor_started = True

        def serve_forever(self, *, poll_interval):
            assert poll_interval == 0.25
            raise RuntimeError("synthetic serve failure")

        def server_close(self):
            self.closed = True

    spy = ServerSpy()
    monkeypatch.setattr(server_module, "CommunityService", lambda paths: object())
    monkeypatch.setattr(server_module, "create_server", lambda service: spy)
    monkeypatch.setattr(server_module.webbrowser, "open", lambda *args, **kwargs: browser_opened)

    with pytest.raises(RuntimeError):
        server_module.run_server(object())

    assert spy.closed is True
    assert spy.monitor_started is browser_opened


@pytest.mark.parametrize(
    "body_headers_and_body",
    [
        b"Content-Length: 2\r\nContent-Length: 3\r\n\r\n{}",
        b"Transfer-Encoding:\r\nTransfer-Encoding: chunked\r\n\r\n",
        b"Content-Length: 2\r\n\r\n{",
    ],
)
def test_rejects_ambiguous_or_incomplete_request_bodies(
    running_server,
    body_headers_and_body,
):
    server, service, token = running_server
    request = (
        b"POST /api/sync HTTP/1.1\r\n"
        + f"Host: {server.expected_authority}\r\n".encode()
        + f"{SESSION_HEADER}: {token}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + body_headers_and_body
    )
    response = _raw_request(server, request)
    assert response.startswith(b"HTTP/1.1 400")
    assert service.calls == []
