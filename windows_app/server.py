"""Authenticated loopback HTTP server for Windows Community Lite."""

from __future__ import annotations

import json
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
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
STARTUP_GRACE_SECONDS = 120.0
BROWSER_IDLE_TIMEOUT_SECONDS = 120.0
IDLE_MONITOR_INTERVAL_SECONDS = 1.0
REQUEST_SOCKET_TIMEOUT_SECONDS = 30.0


class CommunityHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(
        self,
        service: Any,
        session_token: str,
        *,
        clock: Callable[[], float] | None = None,
        startup_grace_seconds: float = STARTUP_GRACE_SECONDS,
        browser_idle_timeout_seconds: float = BROWSER_IDLE_TIMEOUT_SECONDS,
        monitor_interval_seconds: float = IDLE_MONITOR_INTERVAL_SECONDS,
        request_socket_timeout_seconds: float = REQUEST_SOCKET_TIMEOUT_SECONDS,
    ) -> None:
        if (
            min(
                startup_grace_seconds,
                browser_idle_timeout_seconds,
                monitor_interval_seconds,
                request_socket_timeout_seconds,
            )
            <= 0
        ):
            raise ValueError("lifecycle timeouts must be positive")
        super().__init__((LOOPBACK, 0), CommunityRequestHandler)
        self.service = service
        self.session_token = session_token
        self.expected_authority = f"{LOOPBACK}:{self.server_address[1]}"
        self.expected_origin = f"http://{self.expected_authority}"
        self.startup_grace_seconds = float(startup_grace_seconds)
        self.browser_idle_timeout_seconds = float(browser_idle_timeout_seconds)
        self.monitor_interval_seconds = float(monitor_interval_seconds)
        self.request_socket_timeout_seconds = float(request_socket_timeout_seconds)
        self._clock = clock or time.monotonic
        self._lifecycle_lock = threading.Lock()
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._lease_started_at: float | None = None
        self._last_browser_heartbeat: float | None = None
        self._active_api_requests = 0
        self._shutdown_started = False
        self._close_lock = threading.Lock()
        self._closed = threading.Event()

    def get_request(self) -> tuple[Any, Any]:
        """Bound socket reads so a partial authenticated request cannot pin the app."""

        connection, address = super().get_request()
        connection.settimeout(self.request_socket_timeout_seconds)
        return connection, address

    def handle_error(self, request: Any, client_address: Any) -> None:
        # socketserver's default handler prints a traceback that can contain a
        # request URL or response content. The UI receives a generic error.
        return

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()

    @property
    def idle_monitor_running(self) -> bool:
        thread = self._monitor_thread
        return thread is not None and thread.is_alive()

    def start_idle_monitor(self) -> None:
        """Arm browser lease expiry once the UI launch has succeeded."""

        with self._lifecycle_lock:
            if self._monitor_thread is not None or self._shutdown_started:
                return
            self._lease_started_at = self._clock()
            thread = threading.Thread(
                target=self._monitor_idle_browser,
                name="plaud-browser-lease",
                daemon=True,
            )
            self._monitor_thread = thread
            thread.start()

    def note_browser_heartbeat(self) -> bool:
        """Renew the lease only for the authenticated, visible browser UI."""

        with self._lifecycle_lock:
            if self._shutdown_started:
                return False
            self._last_browser_heartbeat = self._clock()
            return True

    def begin_api_request(self) -> bool:
        """Prevent automatic exit while an authenticated request is running."""

        with self._lifecycle_lock:
            if self._shutdown_started:
                return False
            self._active_api_requests += 1
            return True

    def end_api_request(self) -> None:
        with self._lifecycle_lock:
            self._active_api_requests = max(0, self._active_api_requests - 1)

    def check_idle_shutdown(self) -> bool:
        """Stop an unused server, while deferring to an active service job."""

        with self._lifecycle_lock:
            if (
                self._shutdown_started
                or self._active_api_requests
                or not self._browser_lease_expired_locked()
            ):
                return False
            try:
                # This is the service's atomic job/shutdown gate. It prevents a
                # synchronization job from racing with automatic process exit.
                self.service.prepare_shutdown()
            except Exception:  # noqa: BLE001 - retry without logging service details
                return False
            self._shutdown_started = True
            self._monitor_stop.set()

        # BaseServer.shutdown() must run outside the serve_forever thread.
        self.shutdown()
        return True

    def reserve_explicit_shutdown(self) -> dict[str, Any]:
        """Atomically reject exit if another authenticated request is active."""

        with self._lifecycle_lock:
            if self._shutdown_started:
                raise ServiceError("shutting_down", "앱이 종료 중입니다.", status=409)
            # The shutdown request itself is included in this count.
            if self._active_api_requests != 1:
                raise ServiceError(
                    "busy",
                    "진행 중인 요청이 끝난 뒤 앱을 종료하세요.",
                    status=409,
                )
            result = self.service.prepare_shutdown()
            self._shutdown_started = True
            self._monitor_stop.set()
            return result

    def complete_shutdown(self) -> None:
        """Finish a shutdown already reserved by an API or idle monitor."""

        self.shutdown()

    def stop_idle_monitor(self) -> None:
        self._monitor_stop.set()
        thread = self._monitor_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def server_close(self) -> None:
        with self._lifecycle_lock:
            self._shutdown_started = True
        self.stop_idle_monitor()
        with self._close_lock:
            if self._closed.is_set():
                return
            super().server_close()
            self._closed.set()

    def _browser_lease_expired_locked(self) -> bool:
        if self._lease_started_at is None:
            return False
        if self._last_browser_heartbeat is None:
            reference = self._lease_started_at
            timeout = self.startup_grace_seconds
        else:
            reference = self._last_browser_heartbeat
            timeout = self.browser_idle_timeout_seconds
        return self._clock() - reference >= timeout

    def _monitor_idle_browser(self) -> None:
        while not self._monitor_stop.wait(self.monitor_interval_seconds):
            if self.check_idle_shutdown():
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
            if not self.app_server.begin_api_request():
                self._json_error(HTTPStatus.CONFLICT, "shutting_down", "앱이 종료 중입니다.")
                return
            try:
                self._dispatch_api_get(
                    parsed.path,
                    parse_qs(parsed.query, keep_blank_values=True),
                )
            finally:
                self.app_server.end_api_request()
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
        if not self.app_server.begin_api_request():
            self._json_error(HTTPStatus.CONFLICT, "shutting_down", "앱이 종료 중입니다.")
            return
        if parsed.query:
            try:
                self._json_error(
                    HTTPStatus.BAD_REQUEST,
                    "query_not_allowed",
                    "잘못된 요청입니다.",
                )
            finally:
                self.app_server.end_api_request()
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
        finally:
            self.app_server.end_api_request()

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
            elif path == "/api/settings":
                self._json(HTTPStatus.OK, service.integration_settings())
            elif path == "/api/folder-preview":
                self._json(HTTPStatus.OK, service.folder_preview())
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
        if path == "/api/heartbeat":
            _require_exact_fields(body)
            if not self.app_server.note_browser_heartbeat():
                raise ServiceError("shutting_down", "앱이 종료 중입니다.", status=409)
            self._json(
                HTTPStatus.OK,
                {
                    "status": "alive",
                    "idle_timeout_seconds": int(self.app_server.browser_idle_timeout_seconds),
                },
            )
        elif path == "/api/sync":
            _require_exact_fields(body)
            self._json(HTTPStatus.ACCEPTED, service.start_sync())
        elif path == "/api/backfill":
            _require_allowed_fields(body, "limit")
            self._json(HTTPStatus.ACCEPTED, service.start_backfill(limit=body.get("limit")))
        elif path == "/api/import-curl":
            _require_exact_fields(body, "curl")
            self._json(HTTPStatus.OK, service.import_curl(body["curl"]))
        elif path == "/api/disconnect":
            _require_exact_fields(body)
            self._json(HTTPStatus.OK, service.disconnect())
        elif path == "/api/settings-routing":
            _require_exact_fields(body, "provider", "backend", "model_id")
            self._json(
                HTTPStatus.OK,
                service.set_routing_settings(body["provider"], body["backend"], body["model_id"]),
            )
        elif path == "/api/provider-key-set":
            _require_exact_fields(body, "provider", "api_key")
            self._json(
                HTTPStatus.OK,
                service.set_provider_key(body["provider"], body["api_key"]),
            )
        elif path == "/api/provider-key-delete":
            _require_exact_fields(body, "provider")
            self._json(HTTPStatus.OK, service.delete_provider_key(body["provider"]))
        elif path == "/api/folder-preview":
            _require_exact_fields(body, "use_ai", "confirm_external")
            self._json(
                HTTPStatus.ACCEPTED,
                service.start_folder_preview(
                    use_ai=body["use_ai"],
                    confirm_external=body["confirm_external"],
                ),
            )
        elif path == "/api/folder-apply":
            _require_exact_fields(body, "file_ids", "plan_id", "confirm_apply")
            self._json(
                HTTPStatus.ACCEPTED,
                service.start_folder_apply(
                    file_ids=body["file_ids"],
                    plan_id=body["plan_id"],
                    confirm_apply=body["confirm_apply"],
                ),
            )
        elif path == "/api/folder-undo":
            _require_exact_fields(body, "confirm_undo")
            self._json(
                HTTPStatus.ACCEPTED,
                service.start_folder_undo(confirm_undo=body["confirm_undo"]),
            )
        elif path == "/api/elevenlabs-transcribe":
            _require_exact_fields(
                body,
                "file_id",
                "confirm_upload",
                "force",
                "language",
                "num_speakers",
            )
            self._json(
                HTTPStatus.ACCEPTED,
                service.start_elevenlabs_transcription(
                    body["file_id"],
                    confirm_upload=body["confirm_upload"],
                    force=body["force"],
                    language=body["language"],
                    num_speakers=body["num_speakers"],
                ),
            )
        elif path == "/api/export":
            _require_exact_fields(body, "file_id", "kind")
            self._json(
                HTTPStatus.OK,
                service.export(str(body["file_id"]), str(body["kind"])),
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
            _require_exact_fields(body)
            self._json(HTTPStatus.OK, self.app_server.reserve_explicit_shutdown())
            threading.Thread(
                target=self.app_server.complete_shutdown,
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


def _require_allowed_fields(body: dict[str, Any], *fields: str) -> None:
    """Allow optional known fields while rejecting any capability extension."""

    if not set(body).issubset(fields):
        raise ServiceError(
            "invalid_fields",
            "요청 항목이 올바르지 않습니다.",
        )


def create_server(
    service: Any,
    *,
    session_token: str | None = None,
    clock: Callable[[], float] | None = None,
    startup_grace_seconds: float = STARTUP_GRACE_SECONDS,
    browser_idle_timeout_seconds: float = BROWSER_IDLE_TIMEOUT_SECONDS,
    monitor_interval_seconds: float = IDLE_MONITOR_INTERVAL_SECONDS,
    request_socket_timeout_seconds: float = REQUEST_SOCKET_TIMEOUT_SECONDS,
) -> CommunityHTTPServer:
    return CommunityHTTPServer(
        service,
        session_token or secrets.token_urlsafe(32),
        clock=clock,
        startup_grace_seconds=startup_grace_seconds,
        browser_idle_timeout_seconds=browser_idle_timeout_seconds,
        monitor_interval_seconds=monitor_interval_seconds,
        request_socket_timeout_seconds=request_socket_timeout_seconds,
    )


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
        server.start_idle_monitor()
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
