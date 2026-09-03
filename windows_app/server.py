"""Authenticated loopback HTTP server for Windows Community Lite."""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from .service import CommunityService, ServiceError

LOOPBACK = "127.0.0.1"
SESSION_HEADER = "X-Plaud-Session"
MAX_REQUEST_BODY = 256 * 1024
MAX_REQUEST_TARGET = 4096
STATIC_ROOT = Path(__file__).resolve().parent / "static"


class CommunityHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, service: Any, session_token: str) -> None:
        super().__init__((LOOPBACK, 0), CommunityRequestHandler)
        self.service = service
        self.session_token = session_token
        self.expected_authority = f"{LOOPBACK}:{self.server_address[1]}"
        self.expected_origin = f"http://{self.expected_authority}"

    def handle_error(self, request: Any, client_address: Any) -> None:
        # socketserver's default handler prints a traceback that can contain a
        # request URL or response content. The UI receives a generic error.
        return


class CommunityRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PlaudCommunityLite"
    sys_version = ""

    @property
    def app_server(self) -> CommunityHTTPServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def log_error(self, format: str, *args: Any) -> None:
        return

    def version_string(self) -> str:
        return self.server_version

    def do_GET(self) -> None:
        if len(self.path) > MAX_REQUEST_TARGET:
            self._json_error(
                HTTPStatus.REQUEST_URI_TOO_LONG, "request_too_long", "요청이 너무 깁니다."
            )
            return
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/"):
            if not self._authorize_api():
                return
            self._dispatch_api_get(parsed.path, parse_qs(parsed.query, keep_blank_values=True))
            return
        if parsed.query:
            self._json_error(HTTPStatus.BAD_REQUEST, "query_not_allowed", "잘못된 요청입니다.")
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        if len(self.path) > MAX_REQUEST_TARGET:
            self._json_error(
                HTTPStatus.REQUEST_URI_TOO_LONG, "request_too_long", "요청이 너무 깁니다."
            )
            return
        parsed = urlsplit(self.path)
        if not parsed.path.startswith("/api/"):
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "요청을 찾을 수 없습니다.")
            return
        if not self._authorize_api():
            return
        if parsed.query:
            self._json_error(HTTPStatus.BAD_REQUEST, "query_not_allowed", "잘못된 요청입니다.")
            return
        try:
            body = self._read_json()
            self._dispatch_api_post(parsed.path, body)
        except ServiceError as exc:
            self._json_error(exc.status, exc.code, exc.message)
        except Exception:  # noqa: BLE001 - HTTP boundary must not expose internals
            self._json_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "요청을 처리하지 못했습니다.",
            )

    def do_OPTIONS(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/") and not self._authorize_api():
            return
        self._json_error(
            HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "허용되지 않은 요청입니다."
        )

    def do_HEAD(self) -> None:
        self._unsupported_method()

    def do_PUT(self) -> None:
        self._unsupported_method()

    def do_PATCH(self) -> None:
        self._unsupported_method()

    def do_DELETE(self) -> None:
        self._unsupported_method()

    def do_TRACE(self) -> None:
        self._unsupported_method()

    def do_CONNECT(self) -> None:
        self._unsupported_method()

    def _unsupported_method(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/") and not self._authorize_api():
            return
        self._json_error(
            HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "허용되지 않은 요청입니다."
        )

    def _authorize_api(self) -> bool:
        host_values = self.headers.get_all("Host", [])
        if host_values != [self.app_server.expected_authority]:
            self._json_error(HTTPStatus.MISDIRECTED_REQUEST, "invalid_host", "잘못된 요청입니다.")
            return False
        origin_values = self.headers.get_all("Origin", [])
        if len(origin_values) > 1 or (
            origin_values and origin_values[0] != self.app_server.expected_origin
        ):
            self._json_error(HTTPStatus.FORBIDDEN, "invalid_origin", "잘못된 요청입니다.")
            return False
        supplied_values = self.headers.get_all(SESSION_HEADER, [])
        if len(supplied_values) != 1 or not secrets.compare_digest(
            supplied_values[0], self.app_server.session_token
        ):
            self._json_error(
                HTTPStatus.UNAUTHORIZED, "invalid_session", "앱 세션이 만료되었습니다."
            )
            return False
        return True

    def _dispatch_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        service = self.app_server.service
        try:
            if path == "/api/status":
                self._json(HTTPStatus.OK, service.status())
            elif path == "/api/library":
                self._json(
                    HTTPStatus.OK,
                    service.library(
                        limit=_one(query, "limit", "100"),
                        offset=_one(query, "offset", "0"),
                    ),
                )
            elif path == "/api/recording":
                self._json(HTTPStatus.OK, service.recording(_one(query, "id", "")))
            elif path == "/api/search":
                self._json(
                    HTTPStatus.OK,
                    service.search(
                        _one(query, "q", ""),
                        limit=_one(query, "limit", "50"),
                    ),
                )
            else:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "요청을 찾을 수 없습니다.")
        except ServiceError as exc:
            self._json_error(exc.status, exc.code, exc.message)
        except Exception:  # noqa: BLE001 - HTTP boundary must not expose internals
            self._json_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "요청을 처리하지 못했습니다.",
            )

    def _dispatch_api_post(self, path: str, body: dict[str, Any]) -> None:
        service = self.app_server.service
        if path == "/api/sync":
            self._json(HTTPStatus.ACCEPTED, service.start_sync())
        elif path == "/api/backfill":
            self._json(HTTPStatus.ACCEPTED, service.start_backfill(limit=body.get("limit")))
        elif path == "/api/import-curl":
            self._json(HTTPStatus.OK, service.import_curl(body.get("curl", "")))
        elif path == "/api/disconnect":
            self._json(HTTPStatus.OK, service.disconnect())
        elif path == "/api/export":
            self._json(
                HTTPStatus.OK,
                service.export(str(body.get("file_id", "")), str(body.get("kind", ""))),
            )
        elif path == "/api/usage-status":
            _require_exact_fields(body, "file_id", "usage_status")
            self._json(
                HTTPStatus.OK,
                service.set_usage_status(body["file_id"], body["usage_status"]),
            )
        elif path == "/api/tag-add":
            _require_exact_fields(body, "file_id", "tag")
            self._json(HTTPStatus.OK, service.add_tag(body["file_id"], body["tag"]))
        elif path == "/api/tag-remove":
            _require_exact_fields(body, "file_id", "tag")
            self._json(HTTPStatus.OK, service.remove_tag(body["file_id"], body["tag"]))
        elif path == "/api/shutdown":
            self._json(HTTPStatus.OK, service.prepare_shutdown())
            threading.Thread(
                target=self.app_server.shutdown,
                name="plaud-shutdown",
                daemon=True,
            ).start()
        else:
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "요청을 찾을 수 없습니다.")

    def _read_json(self) -> dict[str, Any]:
        transfer_encoding = self.headers.get_all("Transfer-Encoding", [])
        if transfer_encoding:
            raise ServiceError("unsupported_body", "지원하지 않는 요청 형식입니다.", status=400)
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ServiceError("content_type", "JSON 요청만 허용됩니다.", status=415)
        content_lengths = self.headers.get_all("Content-Length", [])
        if (
            len(content_lengths) != 1
            or not content_lengths[0]
            or not content_lengths[0].isascii()
            or not content_lengths[0].isdigit()
        ):
            raise ServiceError("content_length", "요청 크기가 올바르지 않습니다.") from None
        length = int(content_lengths[0])
        if length < 0 or length > MAX_REQUEST_BODY:
            raise ServiceError("body_too_large", "요청이 너무 큽니다.", status=413)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ServiceError("incomplete_body", "요청 본문이 완전하지 않습니다.")
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ServiceError("invalid_json", "JSON 형식을 확인하세요.") from None
        if not isinstance(value, dict):
            raise ServiceError("invalid_json", "JSON 객체만 허용됩니다.")
        return value

    def _serve_static(self, path: str) -> None:
        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
            "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        asset = assets.get(path)
        if asset is None:
            if path == "/favicon.ico":
                self._bytes(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            else:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "요청을 찾을 수 없습니다.")
            return
        filename, content_type = asset
        try:
            payload = (STATIC_ROOT / filename).read_bytes()
        except OSError:
            self._json_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "asset_missing",
                "앱 화면을 불러오지 못했습니다.",
            )
            return
        self._bytes(HTTPStatus.OK, payload, content_type)

    def _json(self, status: int | HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._bytes(status, body, "application/json; charset=utf-8")

    def _json_error(self, status: int | HTTPStatus, code: str, message: str) -> None:
        self._json(status, {"error": code, "message": message})

    def _bytes(self, status: int | HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.close_connection = True
        if body and self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return


def _one(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key)
    if not values:
        return default
    if len(values) != 1:
        raise ServiceError("invalid_parameter", f"{key} 값은 하나만 허용됩니다.")
    return values[0]


def _require_exact_fields(body: dict[str, Any], *fields: str) -> None:
    """Fail closed when a local-write payload is missing or adds capabilities."""

    if set(body) != set(fields):
        raise ServiceError(
            "invalid_fields",
            "요청 항목이 올바르지 않습니다.",
        )


def create_server(service: Any, *, session_token: str | None = None) -> CommunityHTTPServer:
    return CommunityHTTPServer(service, session_token or secrets.token_urlsafe(32))


def launch_url(server: CommunityHTTPServer) -> str:
    token = quote(server.session_token, safe="")
    return f"{server.expected_origin}/#session={token}"


def run_server(paths: Any) -> None:
    service = CommunityService(paths)
    server = create_server(service)
    url = launch_url(server)
    try:
        opened = webbrowser.open(url, new=1, autoraise=True)
        if opened is False:
            raise RuntimeError("browser unavailable")
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
