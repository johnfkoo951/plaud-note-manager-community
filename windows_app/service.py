"""Privacy-bounded Plaud application service for the localhost UI.

Shared ``core`` modules are imported lazily.  This keeps importing
``windows_app`` safe and lets ``launcher.configure_environment`` establish the
Windows data and credential namespace first.

Cloud writes are limited to a separately confirmed, previewed folder-routing
plan. Local metadata, provider settings, and external transcripts stay inside
the Community namespace.
"""

from __future__ import annotations

import hmac
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
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_EXPORT_KINDS = frozenset({"transcript", "summary", "outline", "notes"})
_MAX_CURL_BYTES = 256 * 1024
_MAX_TAG_BYTES = 128
_MAX_PROVIDER_KEY_BYTES = 4096
_MAX_ROUTE_FILES = 200
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_PLAN_ID = re.compile(r"[0-9a-f]{32}\Z")

ROUTING_PROVIDERS = ("claude", "codex", "gemini", "grok")
ROUTING_BACKENDS = ("cli", "api")
API_ONLY_PROVIDERS = frozenset({"gemini", "grok"})
_SECRET_PROVIDER = {
    "claude": "anthropic",
    "codex": "openai",
    "gemini": "gemini",
    "grok": "grok",
    "elevenlabs": "elevenlabs",
}

# These values are deliberately shared with the macOS/CLI local metadata UI.
# They are local organization state only; setting one never mutates Plaud Cloud.
USAGE_STATUSES = (
    "unused",
    "metadata-ready",
    "vault-linked",
    "used-elsewhere",
    "archived",
)
_USAGE_STATUS_SET = frozenset(USAGE_STATUSES)


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
    """Expose local features plus explicitly approved Plaud folder writes."""

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
        self._folder_preview_rows: list[dict[str, Any]] = []
        self._folder_preview_planned_at = 0
        self._folder_preview_plan_id = ""
        self._folder_preview_phase = "none"

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
        row = _active_file(storage, file_id)
        content = storage.get_content_row(file_id)
        payload = _file_row(row)
        payload["cached"] = content is not None
        payload["content"] = _content_row(content) if content is not None else None
        payload["local_metadata"] = _local_metadata(storage, file_id)
        external = storage.get_cmds_transcript(file_id)
        payload["elevenlabs_transcript"] = (
            _external_transcript_row(external) if external is not None else None
        )
        from core.transcribe import transcription_retry_outcome_unknown

        payload["elevenlabs_retry_outcome_unknown"] = transcription_retry_outcome_unknown(
            Path(storage._db_path), file_id
        )
        return payload

    def integration_settings(self) -> dict[str, Any]:
        """Return provider configuration and set/unset state, never key material."""

        from core import app_config
        from core.community_models import model_available
        from core.provider_secrets import api_key_status
        from core.secret_store import CredentialStoreError

        config = app_config.load()
        selected = str(config.get("classify_model") or "claude")
        if selected not in ROUTING_PROVIDERS:
            selected = "claude"
        backends = config.get("backends") if isinstance(config.get("backends"), dict) else {}
        models = config.get("models") if isinstance(config.get("models"), dict) else {}
        providers: list[dict[str, Any]] = []
        for provider in ROUTING_PROVIDERS:
            backend = str(backends.get(provider) or "cli")
            if backend not in ROUTING_BACKENDS:
                backend = "cli"
            if provider in API_ONLY_PROVIDERS:
                backend = "api"
            secret_name = _SECRET_PROVIDER[provider]
            try:
                key_set = bool(api_key_status(secret_name)["configured"])
            except (ValueError, CredentialStoreError):
                key_set = False
            providers.append(
                {
                    "provider": provider,
                    "backend": backend,
                    "model_id": str(models.get(provider) or ""),
                    "api_key_set": key_set,
                    "route_ready": model_available(provider, backend),
                }
            )
        try:
            elevenlabs_key_set = bool(api_key_status("elevenlabs")["configured"])
        except (ValueError, CredentialStoreError):
            elevenlabs_key_set = False
        return {
            "routing": {
                "selected_provider": selected,
                "providers": providers,
                "external_confirmation_required": True,
                "apply_confirmation_required": True,
            },
            "elevenlabs": {
                "api_key_set": elevenlabs_key_set,
                "upload_confirmation_required": True,
                "model": "scribe_v2",
            },
        }

    def set_routing_settings(self, provider: Any, backend: Any, model_id: Any) -> dict[str, Any]:
        provider = _valid_routing_provider(provider)
        if not isinstance(backend, str) or backend not in ROUTING_BACKENDS:
            raise ServiceError("invalid_backend", "인증 방식은 OAuth/CLI 또는 API key여야 합니다.")
        if provider in API_ONLY_PROVIDERS and backend == "cli":
            raise ServiceError(
                "invalid_backend",
                "Gemini와 Grok은 이 앱에서 API key 방식만 지원합니다.",
            )
        if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id.strip()):
            raise ServiceError("invalid_model", "모델 ID 형식을 확인하세요.")

        from core import app_config

        app_config.set_routing_settings(provider, backend, model_id.strip())
        return self.integration_settings()

    def set_provider_key(self, provider: Any, api_key: Any) -> dict[str, Any]:
        secret_name = _valid_secret_provider(provider)
        if not isinstance(api_key, str) or len(api_key.encode("utf-8")) > _MAX_PROVIDER_KEY_BYTES:
            raise ServiceError("invalid_api_key", "API key 형식을 확인하세요.")
        from core.provider_secrets import api_key_status, set_api_key
        from core.secret_store import CredentialStoreError

        try:
            set_api_key(secret_name, api_key)
            return api_key_status(secret_name)
        except ValueError as exc:
            raise ServiceError("invalid_api_key", "API key 형식을 확인하세요.") from exc
        except CredentialStoreError as exc:
            raise ServiceError(
                "credential_store_failed",
                "Windows 보안 저장소에 API key를 저장하지 못했습니다.",
                status=500,
            ) from exc

    def delete_provider_key(self, provider: Any) -> dict[str, Any]:
        secret_name = _valid_secret_provider(provider)
        from core.provider_secrets import api_key_status, delete_api_key
        from core.secret_store import CredentialStoreError

        try:
            delete_api_key(secret_name)
            return api_key_status(secret_name)
        except CredentialStoreError as exc:
            raise ServiceError(
                "credential_store_failed",
                "Windows 보안 저장소에서 API key를 삭제하지 못했습니다.",
                status=500,
            ) from exc

    def folder_preview(self) -> dict[str, Any]:
        """Return the last in-process preview without hidden undo state."""

        undo_available, apply_recovery_required = self._folder_routing_recovery_state()
        with self._state_lock:
            return {
                "items": [dict(row) for row in self._folder_preview_rows],
                "planned_at": self._folder_preview_planned_at,
                "plan_id": self._folder_preview_plan_id,
                "limit": _MAX_ROUTE_FILES,
                "phase": self._folder_preview_phase,
                "undo_available": undo_available,
                "apply_recovery_required": apply_recovery_required,
            }

    def start_folder_preview(
        self,
        *,
        use_ai: Any,
        confirm_external: Any,
    ) -> dict[str, Any]:
        """Build a non-mutating route plan in a protected background job."""

        if not isinstance(use_ai, bool) or not isinstance(confirm_external, bool):
            raise ServiceError("invalid_confirmation", "분류 실행 선택값을 확인하세요.")
        if use_ai and confirm_external is not True:
            raise ServiceError(
                "external_confirmation_required",
                "선택한 AI로 보낼 데이터와 비용 가능성을 확인해야 합니다.",
            )
        if not use_ai and confirm_external:
            raise ServiceError(
                "invalid_confirmation",
                "로컬 전용 분류에는 외부 전송 확인을 사용하지 않습니다.",
            )
        _undo_available, apply_recovery_required = self._folder_routing_recovery_state()
        if apply_recovery_required:
            raise ServiceError(
                "folder_apply_recovery_required",
                "중단된 폴더 적용을 먼저 안정화하세요. 복구 버튼을 누른 뒤 상태를 확인하세요.",
                status=409,
            )

        from core import app_config
        from core.community_models import model_available

        config = app_config.load()
        provider = _valid_routing_provider(str(config.get("classify_model") or "claude"))
        backends = config.get("backends") if isinstance(config.get("backends"), dict) else {}
        models = config.get("models") if isinstance(config.get("models"), dict) else {}
        backend = str(backends.get(provider) or "cli")
        model_id = str(models.get(provider) or "")
        if provider in API_ONLY_PROVIDERS:
            backend = "api"
        if backend not in ROUTING_BACKENDS:
            raise ServiceError("invalid_backend", "저장된 인증 방식을 다시 선택하세요.")
        if use_ai and not model_available(provider, backend):
            route = "로그인된 공급자 CLI" if backend == "cli" else "보호된 API key"
            raise ServiceError(
                "provider_unavailable",
                f"{provider}용 {route}를 사용할 수 없습니다. 설정을 확인하세요.",
                status=409,
            )

        return self._start_job(
            "folder-preview",
            lambda progress: self._preview_folders(
                progress,
                use_ai=use_ai,
                provider=provider,
                backend=backend,
                model_id=model_id,
            ),
        )

    def start_folder_apply(
        self,
        *,
        file_ids: Any,
        plan_id: Any,
        confirm_apply: Any,
    ) -> dict[str, Any]:
        """Apply only exact rows selected from the most recent saved preview."""

        if confirm_apply is not True:
            raise ServiceError(
                "apply_confirmation_required",
                "Plaud Cloud 폴더 이동을 확인해야 합니다.",
            )
        if not isinstance(file_ids, list) or not 1 <= len(file_ids) <= _MAX_ROUTE_FILES:
            raise ServiceError("invalid_selection", "적용할 녹음을 1~200개 선택하세요.")
        selected: list[str] = []
        seen: set[str] = set()
        for value in file_ids:
            file_id = _valid_file_id(value)
            if file_id not in seen:
                selected.append(file_id)
                seen.add(file_id)
        if not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id):
            raise ServiceError(
                "invalid_plan",
                "현재 미리보기 식별값이 없습니다. 미리보기를 다시 실행하세요.",
                status=409,
            )
        with self._state_lock:
            current_plan_id = self._folder_preview_plan_id
            preview_phase = self._folder_preview_phase
            preview_ids = {
                str(row.get("file_id") or "")
                for row in self._folder_preview_rows
                if row.get("folder_id") and float(row.get("confidence") or 0) >= 0.6
            }
        if (
            preview_phase != "preview"
            or not current_plan_id
            or not hmac.compare_digest(current_plan_id, plan_id)
        ):
            raise ServiceError(
                "preview_replaced",
                "현재 표시된 미리보기가 아닙니다. 미리보기를 다시 확인하세요.",
                status=409,
            )
        if not preview_ids or not set(selected).issubset(preview_ids):
            raise ServiceError(
                "invalid_selection",
                "현재 미리보기의 적용 가능한 항목만 선택하세요.",
                status=409,
            )
        return self._start_job(
            "folder-apply",
            lambda progress: self._apply_folders(
                progress,
                file_ids=selected,
                plan_id=plan_id,
            ),
        )

    def start_folder_undo(self, *, confirm_undo: Any) -> dict[str, Any]:
        """Restore the exact folders recorded by the latest successful apply."""

        if confirm_undo is not True:
            raise ServiceError(
                "undo_confirmation_required",
                "최근 Plaud Cloud 폴더 이동을 되돌릴지 확인해야 합니다.",
            )
        if not self._folder_undo_artifacts_exist():
            raise ServiceError(
                "no_undo",
                "되돌릴 최근 폴더 이동 기록이 없습니다.",
                status=409,
            )
        return self._start_job("folder-undo", self._undo_folders)

    def _folder_undo_artifacts_exist(self) -> bool:
        from core.community_router import apply_journal_path, undo_journal_path

        undo_path = self._paths.data_dir / "last_classify.json"
        return any(
            path.is_file()
            for path in (undo_path, apply_journal_path(undo_path), undo_journal_path(undo_path))
        )

    def _folder_routing_recovery_state(self) -> tuple[bool, bool]:
        """Inspect durable artifacts without doing network or mutation work."""

        from core.community_router import apply_journal_path, undo_journal_path

        undo_path = self._paths.data_dir / "last_classify.json"
        apply_recovery_required = apply_journal_path(undo_path).is_file()
        undo_available = not apply_recovery_required and (
            undo_path.is_file() or undo_journal_path(undo_path).is_file()
        )
        return undo_available, apply_recovery_required

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

    def start_elevenlabs_transcription(
        self,
        file_id: Any,
        *,
        confirm_upload: Any,
        force: Any,
        language: Any,
        num_speakers: Any,
    ) -> dict[str, Any]:
        file_id = _valid_file_id(file_id)
        if confirm_upload is not True:
            raise ServiceError(
                "upload_confirmation_required",
                "ElevenLabs 업로드와 비용 가능성을 확인해야 합니다.",
            )
        if not isinstance(force, bool):
            raise ServiceError("invalid_force", "재전사 선택값을 확인하세요.")
        if not isinstance(language, str) or (
            language
            and (len(language) not in (2, 3) or not language.isascii() or not language.isalpha())
        ):
            raise ServiceError("invalid_language", "언어 코드는 ko 또는 kor 형식이어야 합니다.")
        speakers = _bounded_int(num_speakers, minimum=0, maximum=32, label="화자 수")
        storage = self._storage_factory()
        _active_file(storage, file_id)
        from core.transcribe import transcription_retry_outcome_unknown

        retry_outcome_unknown = transcription_retry_outcome_unknown(
            Path(storage._db_path), file_id
        )
        if retry_outcome_unknown and not force:
            raise ServiceError(
                "upload_outcome_unknown",
                "이전 ElevenLabs 업로드 결과를 확인하지 못했습니다. 다시 업로드하면 비용이 "
                "두 번 청구될 수 있습니다. 녹음을 다시 열고 위험을 확인한 뒤 재시도하세요.",
                status=409,
            )
        if storage.get_cmds_transcript(file_id) is not None and not force:
            raise ServiceError(
                "transcript_exists",
                "로컬 ElevenLabs 전사가 이미 있습니다. 다시 업로드하려면 재전사를 확인하세요.",
                status=409,
            )
        return self._start_job(
            "elevenlabs",
            lambda progress: self._transcribe_elevenlabs(
                progress,
                file_id=file_id,
                force=force,
                language=language.lower(),
                num_speakers=speakers,
            ),
        )

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
            messages = {
                "sync": "동기화 중",
                "backfill": "백필 중",
                "elevenlabs": "ElevenLabs 전사 중",
                "folder-preview": "폴더 분류 미리보기 중",
                "folder-apply": "폴더 이동 적용 중",
                "folder-undo": "최근 폴더 이동 되돌리는 중",
            }
            self._operation = Operation(
                name=name,
                state="running",
                message=messages.get(name, "작업 중"),
            )

        def update(done: int, total: int, failed: int = 0) -> None:
            with self._state_lock:
                self._operation.done = done
                self._operation.total = total
                self._operation.failed = failed

        def worker() -> None:
            try:
                message = target(update)
            except ServiceError as exc:
                with self._state_lock:
                    self._operation.state = "failed"
                    self._operation.message = exc.message
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

    def _transcribe_elevenlabs(
        self,
        progress: Callable[[int, int, int], None],
        *,
        file_id: str,
        force: bool,
        language: str,
        num_speakers: int,
    ) -> str:
        from core.transcribe import transcribe_and_store

        progress(0, 1, 0)
        result = transcribe_and_store(
            self._config_loader(),
            file_id,
            storage=self._storage_factory(),
            confirm_upload=True,
            force=force,
            model_id="scribe_v2",
            language_code=language or None,
            num_speakers=num_speakers or None,
        )
        progress(1, 1, 0)
        return f"ElevenLabs 전사 {len(result.get('segments') or [])}개 구간을 이 PC에 저장했습니다."

    def _preview_folders(
        self,
        progress: Callable[[int, int, int], None],
        *,
        use_ai: bool,
        provider: str,
        backend: str,
        model_id: str,
    ) -> str:
        from core.community_router import route_recordings, write_preview_plan

        with self._state_lock:
            self._folder_preview_rows = []
            self._folder_preview_planned_at = 0
            self._folder_preview_plan_id = ""
            self._folder_preview_phase = "none"
        progress(0, 1, 0)
        storage = self._storage_factory()
        with self._client_factory(self._config_loader()) as client:
            report = route_recordings(
                storage,
                client,
                limit=_MAX_ROUTE_FILES,
                use_llm=use_ai,
                provider=provider if use_ai else "",
                backend=backend if use_ai else "",
                confirmed_external=use_ai,
                model_id=model_id if use_ai else None,
            )
            if not report.error:
                write_preview_plan(report, self._paths.data_dir / "auto_folder_preview.json")
        rows = report.public_rows()
        with self._state_lock:
            self._folder_preview_rows = rows
            self._folder_preview_planned_at = report.planned_at
            self._folder_preview_plan_id = report.plan_id if not report.error else ""
            self._folder_preview_phase = "preview" if not report.error else "error"
        progress(len(rows), len(rows), 0)
        if report.error:
            return report.error
        actionable = sum(
            1 for row in rows if row.get("folder_id") and float(row.get("confidence") or 0) >= 0.6
        )
        route_label = "선택한 AI와 로컬 규칙" if use_ai else "로컬 규칙"
        return f"{route_label}으로 {len(rows)}개를 확인해 {actionable}개 이동안을 만들었습니다."

    def _apply_folders(
        self,
        progress: Callable[[int, int, int], None],
        *,
        file_ids: list[str],
        plan_id: str,
    ) -> str:
        from core.community_router import (
            ApplyJournalError,
            PreviewPlanError,
            UndoManifestError,
            apply_saved_plan,
        )

        progress(0, len(file_ids), 0)
        storage = self._storage_factory()
        undo_path = self._paths.data_dir / "last_classify.json"
        with self._client_factory(self._config_loader()) as client:
            try:
                report = apply_saved_plan(
                    storage,
                    client,
                    selected_ids=file_ids,
                    plan_path=self._paths.data_dir / "auto_folder_preview.json",
                    expected_plan_id=plan_id,
                    undo_path=undo_path,
                    min_confidence=0.6,
                )
            except UndoManifestError as exc:
                raise _undo_service_error(exc, applying=True) from exc
            except ApplyJournalError as exc:
                if "recovered an interrupted folder apply" in str(exc).lower():
                    with self._state_lock:
                        self._folder_preview_plan_id = ""
                        self._folder_preview_phase = "recovered"
                    raise ServiceError(
                        "folder_apply_recovered",
                        "중단됐던 폴더 적용을 복구했습니다. 새 미리보기 전에 최근 이동 기록을 확인하세요.",
                        status=409,
                    ) from exc
                raise ServiceError(
                    "folder_apply_recovery_required",
                    "중단된 폴더 적용을 확인할 수 없어 새 변경을 하지 않았습니다. 다시 시도하세요.",
                    status=409,
                ) from exc
            except PreviewPlanError as exc:
                raise ServiceError(
                    "preview_replaced",
                    "미리보기 또는 폴더 상태가 달라져 적용하지 않았습니다. 미리보기를 다시 실행하세요.",
                    status=409,
                ) from exc
        rows = report.public_rows()
        failed = sum(1 for row in rows if not row.get("applied"))
        with self._state_lock:
            self._folder_preview_rows = rows
            self._folder_preview_planned_at = report.planned_at
            self._folder_preview_plan_id = report.plan_id
            self._folder_preview_phase = "applied"
        progress(len(rows), len(rows), failed)
        return f"Plaud Cloud에서 {report.applied_count}개를 이동했습니다. 실패 {failed}개."

    def _undo_folders(self, progress: Callable[[int, int, int], None]) -> str:
        """Delegate exact, journaled restoration to the shared core."""

        from core.community_router import (
            ApplyJournalError,
            UndoManifestError,
            undo_saved_manifest,
        )

        undo_path = self._paths.data_dir / "last_classify.json"
        storage = self._storage_factory()
        progress(0, 1, 0)
        try:
            with self._client_factory(self._config_loader()) as client:
                report = undo_saved_manifest(storage, client, undo_path)
        except (ApplyJournalError, UndoManifestError) as exc:
            if isinstance(exc, ApplyJournalError):
                raise ServiceError(
                    "folder_apply_recovery_required",
                    "중단된 폴더 적용을 확인할 수 없어 새 변경을 하지 않았습니다. 다시 시도하세요.",
                    status=409,
                ) from exc
            raise _undo_service_error(exc) from exc

        public = report.public_dict()
        reverted = int(public.get("reverted") or 0)
        failures = public.get("failed") if isinstance(public.get("failed"), list) else []
        remaining = int(report.remaining_count)
        total = reverted + remaining
        progress(total, total, len(failures))

        if public.get("status") == "apply_recovery_required":
            with self._state_lock:
                self._folder_preview_rows = []
                self._folder_preview_planned_at = 0
                self._folder_preview_plan_id = ""
                self._folder_preview_phase = "recovered"
            return str(public.get("detail") or (
                "중단된 폴더 적용을 안정화했습니다. 상태를 확인한 뒤 되돌리기를 다시 누르세요."
            ))

        if reverted:
            with self._state_lock:
                self._folder_preview_rows = []
                self._folder_preview_planned_at = 0
                self._folder_preview_plan_id = ""
                self._folder_preview_phase = "undone"
        if public.get("status") == "nothing" and not reverted:
            return "되돌릴 최근 폴더 이동 기록이 없습니다."
        if failures:
            return (
                f"Plaud Cloud 폴더 이동 {reverted}개를 되돌렸습니다. "
                f"확인 필요 {len(failures)}개, 남은 기록 {remaining}개."
            )
        return f"Plaud Cloud 폴더 이동 {reverted}개를 되돌렸습니다."

    def prepare_shutdown(self) -> dict[str, str]:
        """Reserve shutdown only while no protected background job is active."""

        with self._state_lock:
            if self._operation.state == "running" or self._job_gate.locked():
                raise ServiceError(
                    "busy",
                    "진행 중인 작업이 끝난 뒤 앱을 종료하세요.",
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
            raise ServiceError(
                "live_check_unavailable",
                "Plaud 연결을 확인하지 못해 인증을 저장하지 않았습니다. 기존 연결 정보는 유지됩니다.",
                status=503,
            )
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

    def set_usage_status(self, file_id: Any, usage_status: Any) -> dict[str, Any]:
        """Set one of the five local workflow states without touching Plaud Cloud."""

        file_id = _valid_file_id(file_id)
        if not isinstance(usage_status, str) or usage_status not in _USAGE_STATUS_SET:
            raise ServiceError(
                "invalid_usage_status",
                "사용 상태가 올바르지 않습니다.",
            )
        storage = self._storage_factory()
        _active_file(storage, file_id)
        storage.update_usage_status(file_id, usage_status, now=int(time.time()))
        return _local_metadata(storage, file_id)

    def add_tag(self, file_id: Any, raw_tag: Any) -> dict[str, Any]:
        """Add one normalized manual tag to the local database only."""

        file_id = _valid_file_id(file_id)
        tag = _valid_tag(raw_tag)
        storage = self._storage_factory()
        _active_file(storage, file_id)
        storage.add_note_tags(file_id, [tag], source="manual", now=int(time.time()))
        return _local_metadata(storage, file_id)

    def remove_tag(self, file_id: Any, raw_tag: Any) -> dict[str, Any]:
        """Remove one normalized tag from the local database only."""

        file_id = _valid_file_id(file_id)
        tag = _valid_tag(raw_tag)
        storage = self._storage_factory()
        _active_file(storage, file_id)
        storage.remove_note_tags(file_id, [tag])
        return _local_metadata(storage, file_id)

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


def _undo_service_error(exc: Exception, *, applying: bool = False) -> ServiceError:
    """Map core recovery failures to stable Korean messages without leaking ids."""

    from core.community_router import UndoJournalError

    detail = str(exc).lower()
    if "replaced" in detail or "manifest changed" in detail:
        return ServiceError(
            "undo_manifest_replaced",
            "되돌리기 기록이 다른 작업에서 바뀌어 새 기록을 덮지 않고 중단했습니다.",
            status=409,
        )
    if "changed" in detail or "no longer matches" in detail:
        return ServiceError(
            "undo_conflict",
            "Plaud Cloud 또는 로컬 폴더 상태가 적용 직후와 달라 아무 항목도 변경하지 않았습니다.",
            status=409,
        )
    if "could not verify" in detail or "unavailable" in detail:
        return ServiceError(
            "undo_preflight_failed",
            "Plaud Cloud 폴더 상태를 확인할 수 없어 아무 항목도 변경하지 않았습니다.",
            status=409,
        )
    if isinstance(exc, UndoJournalError):
        return ServiceError(
            "folder_undo_recovery_required",
            "중단된 되돌리기를 안전하게 복구할 수 없어 새 변경을 하지 않았습니다. 다시 시도하세요.",
            status=409,
        )
    action = "적용하지 않았습니다" if applying else "Plaud Cloud를 변경하지 않았습니다"
    return ServiceError(
        "invalid_undo_manifest",
        f"되돌리기 기록 형식이 올바르지 않아 {action}.",
        status=409,
    )


def _bounded_int(value: Any, *, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ServiceError("invalid_parameter", f"{label} 값이 올바르지 않습니다.") from None
    if not minimum <= parsed <= maximum:
        raise ServiceError("invalid_parameter", f"{label} 범위를 확인하세요.")
    return parsed


def _valid_file_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ServiceError("invalid_file_id", "녹음 식별자가 올바르지 않습니다.")
    value = value.strip()
    if not _FILE_ID.fullmatch(value) or value in {".", ".."}:
        raise ServiceError("invalid_file_id", "녹음 식별자가 올바르지 않습니다.")
    return value


def _valid_routing_provider(value: Any) -> str:
    if not isinstance(value, str) or value not in ROUTING_PROVIDERS:
        raise ServiceError("invalid_provider", "AI 제공자를 확인하세요.")
    return value


def _valid_secret_provider(value: Any) -> str:
    if not isinstance(value, str) or value not in _SECRET_PROVIDER:
        raise ServiceError("invalid_provider", "API key 제공자를 확인하세요.")
    return _SECRET_PROVIDER[value]


def _active_file(storage: Any, file_id: str) -> Mapping[str, Any]:
    row = storage.get_file_row(file_id)
    if row is None or int(row["is_trash"] or 0) != 0:
        raise ServiceError("not_found", "녹음을 찾을 수 없습니다.", status=404)
    return row


def _valid_tag(value: Any) -> str:
    if not isinstance(value, str):
        raise ServiceError("invalid_tag", "태그를 확인하세요.")
    raw = value.strip()
    if not raw or _CONTROL_CHARACTER.search(raw) or len(raw.encode("utf-8")) > _MAX_TAG_BYTES:
        raise ServiceError("invalid_tag", "태그를 확인하세요.")

    # Import lazily so the Windows launcher can establish its isolated runtime
    # before any shared core module is loaded.
    from core.tags import normalize_tags

    normalized = normalize_tags([raw])
    if len(normalized) != 1 or len(normalized[0].encode("utf-8")) > _MAX_TAG_BYTES:
        raise ServiceError("invalid_tag", "태그는 한 번에 하나씩 입력하세요.")
    return normalized[0]


def _local_metadata(storage: Any, file_id: str) -> dict[str, Any]:
    metadata = storage.get_note_metadata(file_id)
    raw_status = metadata["usage_status"] if metadata is not None else "unused"
    usage_status = raw_status if raw_status in _USAGE_STATUS_SET else "unused"
    tags = [str(row["tag"]) for row in storage.list_note_tags(file_id)]
    return {
        "usage_status": usage_status,
        "usage_statuses": list(USAGE_STATUSES),
        "tags": tags,
        "storage": "local-only",
    }


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


def _external_transcript_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "provider": "elevenlabs",
        "model": str(row["model"] or "scribe_v2"),
        "language": row["language"],
        "text": str(row["text"] or ""),
        "segments": _json_value(row["segments"], []),
        "stored_locally": True,
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
