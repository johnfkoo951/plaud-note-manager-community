"""Narrow, read-only Plaud application service for the localhost UI.

Shared ``core`` modules are imported lazily.  This keeps importing
``windows_app`` safe and lets ``launcher.configure_environment`` establish the
Windows data and credential namespace first.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

_FILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_EXPORT_KINDS = frozenset({"transcript", "summary", "outline", "notes"})
_MAX_CURL_BYTES = 256 * 1024


class PathsLike(Protocol):
    data_dir: Path
    export_dir: Path
    env_file: Path


class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class Operation:
    name: str = "none"
    state: str = "idle"
    done: int = 0
    total: int = 0
    failed: int = 0
    message: str = "대기 중"


class CommunityService:
    """Expose only local writes and Plaud Cloud read operations."""

    def __init__(
        self,
        paths: PathsLike,
        *,
        storage_factory: Callable[[], Any] | None = None,
        config_loader: Callable[[], Any] | None = None,
        client_factory: Callable[[Any], Any] | None = None,
        auth_loader: Callable[[], Any] | None = None,
        auth_refresher: Callable[[str, Path], Any] | None = None,
        credential_disconnect: Callable[[Path], None] | None = None,
    ) -> None:
        self._paths = paths
        self._storage_factory = storage_factory or self._default_storage
        self._config_loader = config_loader or self._default_config
        self._client_factory = client_factory or self._default_client
        self._auth_loader = auth_loader or self._default_auth
        self._auth_refresher = auth_refresher or self._default_auth_refresher
        self._credential_disconnect = credential_disconnect or self._default_disconnect
        self._operation = Operation()
        self._state_lock = threading.Lock()
        self._job_gate = threading.Lock()
        self._shutdown_requested = False

    # Lazy defaults: launcher isolation must already be active when called.

    @staticmethod
    def _default_storage() -> Any:
        from core.storage import Storage

        return Storage()

    @staticmethod
    def _default_config() -> Any:
        from core.config import load_config

        return load_config()

    @staticmethod
    def _default_client(config: Any) -> Any:
        from core.client import PlaudClient

        return PlaudClient(config)

    @staticmethod
    def _default_auth() -> Any:
        from core.auth_status import auth_status

        return auth_status(live=False)

    @staticmethod
    def _default_auth_refresher(curl_text: str, env_path: Path) -> Any:
        from core.refresh_auth import refresh_auth

        return refresh_auth(env_path=env_path, curl_text=curl_text, validate_live=True)

    @staticmethod
    def _default_disconnect(env_path: Path) -> None:
        from core.secret_store import disconnect_community_credentials

        disconnect_community_credentials(env_path)

    def operation(self) -> dict[str, Any]:
        with self._state_lock:
            return asdict(self._operation)

    def status(self) -> dict[str, Any]:
        storage = self._storage_factory()
        try:
            counts = storage.counts()
        except Exception:  # noqa: BLE001 - status remains generic across storage backends
            counts = {"total": 0, "trash": 0, "folders": 0, "cached": 0}

        try:
            auth = self._auth_loader()
            auth_payload = {
                "configured": bool(auth.configured),
                "state": str(auth.state),
                "remaining": auth.remaining_human,
            }
        except Exception:  # noqa: BLE001 - credential backend details must stay private
            auth_payload = {"configured": False, "state": "unavailable", "remaining": None}

        return {
            "edition": "community-windows-lite",
            "auth": auth_payload,
            "library": {
                "total": int(counts.get("total", 0)),
                "trash": int(counts.get("trash", 0)),
                "folders": int(counts.get("folders", 0)),
                "cached": int(counts.get("cached", 0)),
            },
            "operation": self.operation(),
        }

    def library(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        limit = _bounded_int(limit, minimum=1, maximum=200, label="limit")
        offset = _bounded_int(offset, minimum=0, maximum=1_000_000, label="offset")
        storage = self._storage_factory()
        with storage._connect() as conn:  # shared core currently has no public list query
            rows = conn.execute(
                """
                SELECT f.id, f.filename, f.duration, f.edit_time, f.start_time,
                       f.starred, fc.file_id IS NOT NULL AS cached,
                       GROUP_CONCAT(fd.name, ' · ') AS folders
                  FROM files AS f
             LEFT JOIN file_content AS fc ON fc.file_id = f.id
             LEFT JOIN file_folders AS ff ON ff.file_id = f.id
             LEFT JOIN folders AS fd ON fd.id = ff.folder_id
                 WHERE f.is_trash = 0
              GROUP BY f.id
              ORDER BY COALESCE(f.edit_time, f.start_time, 0) DESC
                 LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return {"items": [_file_row(row) for row in rows], "limit": limit, "offset": offset}

    def recording(self, file_id: str) -> dict[str, Any]:
        file_id = _valid_file_id(file_id)
        storage = self._storage_factory()
        row = storage.get_file_row(file_id)
        if row is None or int(row["is_trash"] or 0) != 0:
            raise ServiceError("not_found", "녹음을 찾을 수 없습니다.", status=404)
        content = storage.get_content_row(file_id)
        payload = _file_row(row)
        payload["cached"] = content is not None
        payload["content"] = _content_row(content) if content is not None else None
        return payload

    def search(self, query: str, *, limit: int = 50) -> dict[str, Any]:
        query = str(query).strip()
        if not query or len(query) > 200:
            raise ServiceError("invalid_query", "검색어는 1~200자로 입력하세요.")
        limit = _bounded_int(limit, minimum=1, maximum=100, label="limit")
        storage = self._storage_factory()
        results = []
        for hit in storage.search_recordings(query, limit=limit):
            row = storage.get_file_row(hit["file_id"])
            if row is None or int(row["is_trash"] or 0) != 0:
                continue
            results.append(
                {
                    "id": hit["file_id"],
                    "title": row["filename"] or "제목 없는 녹음",
                    "snippet": str(hit.get("snippet") or "")[:500],
                }
            )
        return {"query": query, "items": results}

    def start_sync(self) -> dict[str, Any]:
        return self._start_job("sync", self._sync_metadata)

    def start_backfill(self, *, limit: int | None = None) -> dict[str, Any]:
        if limit is not None:
            limit = _bounded_int(limit, minimum=1, maximum=10_000, label="limit")
        return self._start_job("backfill", lambda progress: self._backfill(progress, limit=limit))

    def _start_job(
        self,
        name: str,
        target: Callable[[Callable[[int, int, int], None]], str],
    ) -> dict[str, Any]:
        if not self._job_gate.acquire(blocking=False):
            raise ServiceError("busy", "다른 작업이 진행 중입니다.", status=409)

        with self._state_lock:
            if self._shutdown_requested:
                self._job_gate.release()
                raise ServiceError("shutting_down", "앱이 종료 중입니다.", status=409)
            self._operation = Operation(
                name=name,
                state="running",
                message="동기화 중" if name == "sync" else "백필 중",
            )

        def update(done: int, total: int, failed: int = 0) -> None:
            with self._state_lock:
                self._operation.done = done
                self._operation.total = total
                self._operation.failed = failed

        def worker() -> None:
            try:
                message = target(update)
            except Exception:  # noqa: BLE001 - worker result deliberately redacts internals
                with self._state_lock:
                    self._operation.state = "failed"
                    self._operation.message = (
                        "작업을 완료하지 못했습니다. 인증과 네트워크를 확인하세요."
                    )
            else:
                with self._state_lock:
                    self._operation.state = "succeeded"
                    self._operation.message = message
            finally:
                self._job_gate.release()

        try:
            threading.Thread(target=worker, name=f"plaud-{name}", daemon=True).start()
        except BaseException:
            self._job_gate.release()
            raise
        return self.operation()

    def prepare_shutdown(self) -> dict[str, str]:
        """Reserve shutdown only while no local synchronization write is active."""

        with self._state_lock:
            if self._operation.state == "running" or self._job_gate.locked():
                raise ServiceError(
                    "busy",
                    "동기화가 끝난 뒤 앱을 종료하세요.",
                    status=409,
                )
            self._shutdown_requested = True
        return {"status": "shutting_down"}

    def _sync_metadata(self, progress: Callable[[int, int, int], None]) -> str:
        config = self._config_loader()
        storage = self._storage_factory()
        now = int(time.time())
        synced = 0
        with self._client_factory(config) as client:
            folders = client.list_folders()
            storage.replace_folders(folders, now=now)
            progress(0, 2, 0)
            for step, is_trash in enumerate((0, 1), start=1):
                skip = 0
                while True:
                    page = client.list_files(limit=2000, skip=skip, is_trash=is_trash)
                    items = list(page.items)
                    for item in items:
                        storage.upsert_file(item, now=now, is_trash=is_trash)
                        synced += 1
                    next_skip = skip + len(items)
                    try:
                        total = max(0, int(page.total))
                    except (AttributeError, TypeError, ValueError):
                        total = next_skip
                    if not items or next_skip >= total:
                        break
                    skip = next_skip
                progress(step, 2, 0)
        return f"목록 {synced}개를 동기화했습니다."

    def _backfill(
        self,
        progress: Callable[[int, int, int], None],
        *,
        limit: int | None,
    ) -> str:
        storage = self._storage_factory()
        pending = list(storage.files_without_content())
        if limit is not None:
            pending = pending[:limit]
        total = len(pending)
        progress(0, total, 0)
        if not pending:
            return "이미 필요한 내용이 캐시되어 있습니다."

        config = self._config_loader()
        failed = 0
        with self._client_factory(config) as client:
            for index, row in enumerate(pending, start=1):
                try:
                    content = client.file_content(str(row["id"]))
                    storage.save_content(content, now=int(time.time()))
                except Exception:  # noqa: BLE001 - one failed read must not abort the backlog
                    failed += 1
                progress(index, total, failed)
        succeeded = total - failed
        return f"전사·요약 {succeeded}개를 저장했습니다. 실패 {failed}개."

    def import_curl(self, curl_text: str) -> dict[str, Any]:
        if not isinstance(curl_text, str) or not curl_text.strip():
            raise ServiceError("empty_curl", "Plaud에서 복사한 cURL을 입력하세요.")
        if len(curl_text.encode("utf-8")) > _MAX_CURL_BYTES:
            raise ServiceError("curl_too_large", "cURL 입력이 너무 큽니다.", status=413)
        try:
            result = self._auth_refresher(curl_text, self._paths.env_file)
        except Exception:  # noqa: BLE001 - never expose credential backend details
            raise ServiceError(
                "credential_store_failed",
                "Windows 보안 저장소에 인증을 저장하지 못했습니다.",
                status=500,
            ) from None
        if result.status == "ok":
            return {
                "status": "connected",
                "verification": "verified",
                "cookie_captured": bool(result.cookie_captured),
            }
        if result.status == "live_check_unavailable":
            return {
                "status": "connected_unverified",
                "verification": "unreachable",
                "cookie_captured": bool(result.cookie_captured),
                "message": "인증은 저장했지만 네트워크 문제로 확인하지 못했습니다.",
            }
        if result.status == "live_auth_failed":
            raise ServiceError(
                "live_auth_failed",
                "Plaud가 이 인증을 거부했습니다. 기존 연결 정보는 변경하지 않았습니다.",
                status=401,
            )
        if result.status in {"invalid_curl", "clipboard_empty"}:
            raise ServiceError("invalid_curl", "Plaud API cURL 형식을 확인하세요.")
        if result.status == "write_failed":
            raise ServiceError(
                "credential_store_failed",
                "Windows 보안 저장소에 인증을 저장하지 못했습니다.",
                status=500,
            )
        raise ServiceError("auth_failed", "연결 정보를 확인하지 못했습니다.", status=500)

    def disconnect(self) -> dict[str, str]:
        try:
            self._credential_disconnect(self._paths.env_file)
        except Exception:  # noqa: BLE001 - never expose credential backend details
            raise ServiceError(
                "disconnect_failed", "연결 정보를 삭제하지 못했습니다.", status=500
            ) from None
        return {"status": "disconnected"}

    def export(self, file_id: str, kind: str) -> dict[str, str]:
        file_id = _valid_file_id(file_id)
        if kind not in _EXPORT_KINDS:
            raise ServiceError("invalid_export", "내보내기 종류가 올바르지 않습니다.")
        storage = self._storage_factory()
        row = storage.get_content_row(file_id)
        if row is None:
            raise ServiceError("not_cached", "먼저 이 녹음을 백필하세요.", status=409)
        text = _export_text(row, kind)
        self._paths.export_dir.mkdir(parents=True, exist_ok=True)
        target = self._paths.export_dir / f"recording-{file_id}-{kind}.md"
        _atomic_private_write(target, text)
        return {"status": "exported", "file": target.name, "directory": str(target.parent)}


def _bounded_int(value: Any, *, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ServiceError("invalid_parameter", f"{label} 값이 올바르지 않습니다.") from None
    if not minimum <= parsed <= maximum:
        raise ServiceError("invalid_parameter", f"{label} 범위를 확인하세요.")
    return parsed


def _valid_file_id(value: Any) -> str:
    value = str(value or "")
    if not _FILE_ID.fullmatch(value) or value in {".", ".."}:
        raise ServiceError("invalid_file_id", "녹음 식별자가 올바르지 않습니다.")
    return value


def _file_row(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": str(row["id"]),
        "title": row["filename"] or "제목 없는 녹음",
        "duration": row["duration"],
        "edit_time": row["edit_time"],
        "start_time": row["start_time"],
        "starred": bool(row["starred"]) if "starred" in keys else False,
        "cached": bool(row["cached"]) if "cached" in keys else False,
        "folders": row["folders"] if "folders" in keys else None,
    }


def _json_value(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _content_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "title": row["title"],
        "transcript": _json_value(row["transcript"], []),
        "outline": _json_value(row["outline"], []),
        "summary": row["summary_md"] or "",
        "notes": _json_value(row["summary_extra"], []),
        "keywords": _json_value(row["keywords"], []),
        "fetched_at": row["fetched_at"],
    }


def _timestamp(milliseconds: Any) -> str:
    try:
        seconds = max(0, int(milliseconds) // 1000)
    except (TypeError, ValueError):
        seconds = 0
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _export_text(row: Mapping[str, Any], kind: str) -> str:
    if kind == "summary":
        return str(row["summary_md"] or "")
    if kind == "transcript":
        segments = _json_value(row["transcript"], [])
        lines = []
        for segment in segments if isinstance(segments, list) else []:
            if not isinstance(segment, dict):
                continue
            speaker = str(segment.get("speaker") or "").strip()
            content = str(segment.get("content") or "").strip()
            prefix = f"{speaker}: " if speaker else ""
            lines.append(f"[{_timestamp(segment.get('start_time'))}] {prefix}{content}".rstrip())
        return "\n".join(lines)
    if kind == "outline":
        outline = _json_value(row["outline"], [])
        return "\n".join(
            f"- [{_timestamp(item.get('start_time'))}] {str(item.get('topic') or '').strip()}"
            for item in outline
            if isinstance(item, dict)
        )
    notes = [str(row["summary_md"] or "")]
    extras = _json_value(row["summary_extra"], [])
    if isinstance(extras, list):
        notes.extend(str(item) for item in extras if item)
    return "\n\n---\n\n".join(item for item in notes if item)


def _atomic_private_write(path: Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temp, flags, 0o600)
    try:
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
