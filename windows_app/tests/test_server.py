from __future__ import annotations

import http.client
import io
import json
import socket
import threading
from contextlib import redirect_stderr

import pytest

from windows_app.server import SESSION_HEADER, create_server, launch_url


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

    def prepare_shutdown(self):
        self.calls.append("shutdown")
        return {"status": "shutting_down"}


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
        ("POST", "/api/sync", {}),
        ("POST", "/api/backfill", {}),
        ("POST", "/api/import-curl", {"curl": "secret"}),
        ("POST", "/api/disconnect", {}),
        ("POST", "/api/export", {"file_id": "rec-1", "kind": "summary"}),
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
        ("POST", "/api/sync", {}, 202, "sync"),
        ("POST", "/api/backfill", {"limit": 25}, 202, ("backfill", {"limit": 25})),
        ("POST", "/api/import-curl", {"curl": "candidate"}, 200, ("import", "candidate")),
        ("POST", "/api/disconnect", {}, 200, "disconnect"),
        (
            "POST",
            "/api/export",
            {"file_id": "rec-1", "kind": "summary"},
            200,
            ("export", "rec-1", "summary"),
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
