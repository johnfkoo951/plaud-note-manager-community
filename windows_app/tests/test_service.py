from __future__ import annotations

import inspect
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.storage import Storage
from core.models import Folder
from windows_app.service import CommunityService, ServiceError, USAGE_STATUSES


@dataclass
class AppPaths:
    data_dir: Path
    export_dir: Path
    env_file: Path


def _read_storage(tmp_path):
    storage = Storage(tmp_path / "plaud.db")
    with storage._connect() as conn:
        conn.execute(
            """
            INSERT INTO files
                (id, filename, duration, edit_time, start_time, is_trash,
                 status, starred, synced_at, updated_at)
            VALUES ('rec-1', '참가자 회의', 125, 1700000000, 1699999000,
                    0, 'new', 0, 1700000100, 1700000100)
            """
        )
        conn.execute(
            """
            INSERT INTO folders (id, name, icon, color, synced_at)
            VALUES ('folder-1', '업무', NULL, NULL, 1700000100)
            """
        )
        conn.execute("INSERT INTO file_folders VALUES ('rec-1', 'folder-1')")
        conn.execute(
            "INSERT INTO file_content VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "rec-1",
                "참가자 회의",
                json.dumps(
                    [
                        {
                            "start_time": 0,
                            "end_time": 1000,
                            "speaker": "A",
                            "content": "안녕하세요",
                        }
                    ],
                    ensure_ascii=False,
                ),
                json.dumps([{"start_time": 0, "end_time": 1000, "topic": "시작"}]),
                "결정 사항",
                json.dumps(["추가 노트"], ensure_ascii=False),
                json.dumps(["회의"], ensure_ascii=False),
                1700000100,
            ),
        )
    storage.rebuild_search_index()
    return storage


def _paths(tmp_path):
    export_dir = tmp_path / "exports"
    export_dir.mkdir(exist_ok=True)
    return AppPaths(tmp_path, export_dir, tmp_path / "settings.env")


def _undo_fixture(tmp_path):
    from core.community_router import replace_undo_manifest

    storage = _read_storage(tmp_path)
    target = Folder(id="folder-target", name="자동 분류")
    previous = Folder(id="folder-previous", name="기존 폴더")
    storage.replace_folders([target, previous], now=1700000200)
    with storage._connect() as conn:
        conn.execute(
            """
            INSERT INTO files
                (id, filename, duration, edit_time, start_time, is_trash,
                 status, starred, synced_at, updated_at)
            VALUES ('rec-2', '두 번째 녹음', 60, 1700000001, 1700000000,
                    0, 'new', 0, 1700000200, 1700000200)
            """
        )
    storage.set_file_folders("rec-1", [target.id])
    storage.set_file_folders("rec-2", [target.id])
    moved = [
        {
            "file_id": "rec-1",
            "folder_id": target.id,
            "folder_name": target.name,
            "title": "참가자 회의",
            "previous_folder_ids": [],
        },
        {
            "file_id": "rec-2",
            "folder_id": target.id,
            "folder_name": target.name,
            "title": "두 번째 녹음",
            "previous_folder_ids": [previous.id],
        },
    ]
    manifest_path = tmp_path / "last_classify.json"
    replace_undo_manifest(moved, manifest_path)
    return storage, [target, previous], moved, manifest_path


def _auth():
    return SimpleNamespace(configured=True, state="valid", remaining_human="2h")


class UndoClient:
    def __init__(self, folders, remote_folders, *, fail_ids=(), on_detail=None):
        self.folders = list(folders)
        self.remote_folders = {
            file_id: list(folder_ids) for file_id, folder_ids in remote_folders.items()
        }
        self.fail_ids = set(fail_ids)
        self.on_detail = on_detail
        self.detail_calls = []
        self.mutations = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def list_folders(self):
        return list(self.folders)

    def file_detail(self, file_id):
        self.detail_calls.append(file_id)
        if self.on_detail is not None:
            self.on_detail(file_id, len(self.detail_calls))
        return {"data": {"filetag_id_list": list(self.remote_folders[file_id])}}

    def set_file_folders_once(self, file_id, folder_ids):
        self.mutations.append((file_id, list(folder_ids)))
        if file_id in self.fail_ids:
            raise RuntimeError("synthetic Cloud failure")
        self.remote_folders[file_id] = list(folder_ids)


def test_local_library_recording_search_and_export(tmp_path):
    storage = _read_storage(tmp_path)
    service = CommunityService(_paths(tmp_path), storage_factory=lambda: storage, auth_loader=_auth)

    listing = service.library(limit=20, offset=0)
    assert listing["items"][0]["id"] == "rec-1"
    assert listing["items"][0]["folders"] == "업무"
    assert listing["items"][0]["cached"] is True

    recording = service.recording("rec-1")
    assert recording["content"]["summary"] == "결정 사항"
    assert recording["content"]["transcript"][0]["content"] == "안녕하세요"
    assert recording["local_metadata"] == {
        "usage_status": "unused",
        "usage_statuses": list(USAGE_STATUSES),
        "tags": [],
        "storage": "local-only",
    }
    assert service.search("회의")["items"][0]["id"] == "rec-1"

    exported = service.export("rec-1", "transcript")
    target = _paths(tmp_path).export_dir / exported["file"]
    assert target.parent == (tmp_path / "exports")
    assert "안녕하세요" in target.read_text(encoding="utf-8")


def test_local_usage_status_and_manual_tags_never_call_plaud_cloud(tmp_path):
    storage = _read_storage(tmp_path)

    def cloud_client_must_not_run(config):
        raise AssertionError("local metadata attempted to create a Plaud client")

    service = CommunityService(
        _paths(tmp_path),
        storage_factory=lambda: storage,
        client_factory=cloud_client_must_not_run,
        auth_loader=_auth,
    )

    for usage_status in USAGE_STATUSES:
        result = service.set_usage_status("rec-1", usage_status)
        assert result["usage_status"] == usage_status
        assert result["storage"] == "local-only"

    result = service.add_tag("rec-1", "#연구 계획")
    assert result["tags"] == ["연구-계획"]
    service.add_tag("rec-1", "연구 계획")  # idempotent duplicate
    assert [row["source"] for row in storage.list_note_tags("rec-1")] == ["manual"]

    result = service.remove_tag("rec-1", "연구 계획")
    assert result["tags"] == []


def test_recording_exposes_locally_stored_elevenlabs_transcript(tmp_path):
    storage = _read_storage(tmp_path)
    storage.save_cmds_transcript(
        file_id="rec-1",
        model="scribe_v2",
        language="ko",
        text="안녕하세요",
        segments_json=json.dumps(
            [
                {
                    "speaker": "speaker_0",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "content": "안녕하세요",
                }
            ],
            ensure_ascii=False,
        ),
        now=1700000200,
    )
    service = CommunityService(_paths(tmp_path), storage_factory=lambda: storage, auth_loader=_auth)

    external = service.recording("rec-1")["elevenlabs_transcript"]

    assert external["provider"] == "elevenlabs"
    assert external["model"] == "scribe_v2"
    assert external["language"] == "ko"
    assert external["segments"][0]["content"] == "안녕하세요"


def test_integration_settings_never_return_secret_material(tmp_path, monkeypatch):
    from core import app_config, community_models, provider_secrets

    monkeypatch.setattr(app_config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(
        provider_secrets,
        "api_key_status",
        lambda provider: {
            "provider": provider,
            "configured": provider in {"openai", "elevenlabs"},
            "status": "set",
            "secret_disclosed": False,
        },
    )
    monkeypatch.setattr(
        community_models,
        "model_available",
        lambda provider, backend: provider == "codex" and backend == "cli",
    )
    service = CommunityService(_paths(tmp_path), auth_loader=_auth)

    settings = service.integration_settings()
    encoded = json.dumps(settings)

    codex = next(item for item in settings["routing"]["providers"] if item["provider"] == "codex")
    assert codex["api_key_set"] is True
    assert settings["elevenlabs"]["api_key_set"] is True
    assert '"api_key":' not in encoded
    assert "test-live-secret-value" not in encoded


def test_routing_settings_validate_and_persist_selected_provider(tmp_path, monkeypatch):
    from core import app_config, community_models, provider_secrets

    monkeypatch.setattr(app_config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(
        provider_secrets,
        "api_key_status",
        lambda provider: {"provider": provider, "configured": False},
    )
    monkeypatch.setattr(community_models, "model_available", lambda provider, backend: False)
    service = CommunityService(_paths(tmp_path), auth_loader=_auth)

    settings = service.set_routing_settings("codex", "api", "gpt-test")

    assert settings["routing"]["selected_provider"] == "codex"
    assert app_config.load()["backends"]["codex"] == "api"
    assert app_config.load()["models"]["codex"] == "gpt-test"
    with pytest.raises(ServiceError, match="AI 제공자"):
        service.set_routing_settings("unknown", "api", "model")


def test_elevenlabs_job_requires_confirmation_and_holds_job_gate(tmp_path, monkeypatch):
    import core.transcribe as transcribe

    storage = _read_storage(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def fake_transcribe(cfg, file_id, *, storage, **kwargs):
        started.set()
        assert release.wait(timeout=2)
        storage.save_cmds_transcript(
            file_id=file_id,
            model="scribe_v2",
            language="ko",
            text="테스트",
            segments_json="[]",
            now=1700000200,
        )
        return {"segments": []}

    monkeypatch.setattr(transcribe, "transcribe_and_store", fake_transcribe)
    service = CommunityService(
        _paths(tmp_path),
        storage_factory=lambda: storage,
        config_loader=lambda: object(),
        auth_loader=_auth,
    )

    with pytest.raises(ServiceError) as exc_info:
        service.start_elevenlabs_transcription(
            "rec-1", confirm_upload=False, force=False, language="ko", num_speakers=0
        )
    assert exc_info.value.code == "upload_confirmation_required"

    service.start_elevenlabs_transcription(
        "rec-1", confirm_upload=True, force=False, language="ko", num_speakers=2
    )
    assert started.wait(timeout=1)
    with pytest.raises(ServiceError) as shutdown_error:
        service.prepare_shutdown()
    assert shutdown_error.value.code == "busy"
    release.set()
    assert _wait_for_job(service)["state"] == "succeeded"
    assert storage.get_cmds_transcript("rec-1") is not None


def test_elevenlabs_unknown_attempt_is_visible_and_blocks_unconfirmed_retry(
    tmp_path, monkeypatch
):
    import core.transcribe as transcribe

    storage = _read_storage(tmp_path)
    marker = transcribe._transcription_attempt_path(storage._db_path, "rec-1")
    transcribe._write_attempt_marker(marker, model_id="scribe_v2")
    monkeypatch.setattr(
        transcribe,
        "transcribe_and_store",
        lambda *_args, **_kwargs: pytest.fail("unknown retry must stop before upload"),
    )
    service = CommunityService(
        _paths(tmp_path),
        storage_factory=lambda: storage,
        config_loader=lambda: object(),
        auth_loader=_auth,
    )

    assert service.recording("rec-1")["elevenlabs_retry_outcome_unknown"] is True
    with pytest.raises(ServiceError) as exc_info:
        service.start_elevenlabs_transcription(
            "rec-1",
            confirm_upload=True,
            force=False,
            language="ko",
            num_speakers=0,
        )
    assert exc_info.value.code == "upload_outcome_unknown"
    assert exc_info.value.status == 409
    assert "두 번" in exc_info.value.message


def test_folder_routing_previews_then_applies_only_selected_existing_folder(tmp_path, monkeypatch):
    from core import app_config

    storage = _read_storage(tmp_path)
    folder = Folder(id="folder-1", name="회의")
    storage.replace_folders([folder], now=1700000200)
    storage.set_file_folders("rec-1", [])

    class RoutingClient:
        def __init__(self):
            self.mutations = []
            self.remote_folders = {"rec-1": []}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def list_folders(self):
            return [folder]

        def set_file_folders_once(self, file_id, folder_ids):
            self.mutations.append((file_id, list(folder_ids)))
            self.remote_folders[file_id] = list(folder_ids)

        def file_detail(self, file_id):
            return {"data": {"filetag_id_list": list(self.remote_folders[file_id])}}

    client = RoutingClient()
    monkeypatch.setattr(app_config, "CONFIG_FILE", tmp_path / "config.json")
    service = CommunityService(
        _paths(tmp_path),
        storage_factory=lambda: storage,
        config_loader=lambda: object(),
        client_factory=lambda _config: client,
        auth_loader=_auth,
    )

    with pytest.raises(ServiceError) as confirmation_error:
        service.start_folder_preview(use_ai=True, confirm_external=False)
    assert confirmation_error.value.code == "external_confirmation_required"
    assert client.mutations == []

    service.start_folder_preview(use_ai=False, confirm_external=False)
    assert _wait_for_job(service)["state"] == "succeeded"
    preview = service.folder_preview()
    assert preview["phase"] == "preview"
    assert len(preview["plan_id"]) == 32
    assert preview["items"][0]["file_id"] == "rec-1"
    assert preview["items"][0]["folder_id"] == "folder-1"
    assert preview["items"][0]["folder_name"] == "회의"
    assert client.mutations == []

    with pytest.raises(ServiceError) as apply_error:
        service.start_folder_apply(
            file_ids=["rec-1"],
            plan_id=preview["plan_id"],
            confirm_apply=False,
        )
    assert apply_error.value.code == "apply_confirmation_required"
    service.start_folder_apply(
        file_ids=["rec-1"],
        plan_id=preview["plan_id"],
        confirm_apply=True,
    )
    assert _wait_for_job(service)["state"] == "succeeded"
    assert client.mutations == [("rec-1", ["folder-1"])]
    assert service.folder_preview()["items"][0]["applied"] is True
    manifest = json.loads((tmp_path / "last_classify.json").read_text())
    assert manifest["schema_version"] == 2
    assert manifest["moved"] == [
        {
            "file_id": "rec-1",
            "folder_id": "folder-1",
            "folder_name": "회의",
            "title": "참가자 회의",
            "previous_folder_ids": [],
        }
    ]

    with pytest.raises(ServiceError) as undo_confirmation_error:
        service.start_folder_undo(confirm_undo=False)
    assert undo_confirmation_error.value.code == "undo_confirmation_required"
    assert client.mutations == [("rec-1", ["folder-1"])]

    service.start_folder_undo(confirm_undo=True)
    undo_result = _wait_for_job(service)
    assert undo_result["state"] == "succeeded"
    assert undo_result["failed"] == 0
    assert client.mutations == [("rec-1", ["folder-1"]), ("rec-1", [])]
    with storage._connect() as conn:
        assert (
            conn.execute("SELECT folder_id FROM file_folders WHERE file_id = 'rec-1'").fetchall()
            == []
        )
    assert not (tmp_path / "last_classify.json").exists()
    assert service.folder_preview()["phase"] == "undone"
    assert service.folder_preview()["undo_available"] is False


def test_folder_undo_requires_a_current_manifest(tmp_path):
    service = CommunityService(_paths(tmp_path), auth_loader=_auth)

    with pytest.raises(ServiceError) as exc_info:
        service.start_folder_undo(confirm_undo=True)

    assert exc_info.value.code == "no_undo"
    assert service.operation()["state"] == "idle"


def test_folder_preview_reports_apply_recovery_without_network_or_mutation(tmp_path):
    from core import community_router

    undo_path = tmp_path / "last_classify.json"
    apply_journal = community_router.apply_journal_path(undo_path)
    apply_journal.write_text("pending", encoding="utf-8")
    service = CommunityService(
        _paths(tmp_path),
        storage_factory=lambda: (_ for _ in ()).throw(AssertionError("must stay read-only")),
        config_loader=lambda: (_ for _ in ()).throw(AssertionError("must stay offline")),
        auth_loader=_auth,
    )

    payload = service.folder_preview()

    assert payload["undo_available"] is False
    assert payload["apply_recovery_required"] is True
    assert apply_journal.read_text(encoding="utf-8") == "pending"

    with pytest.raises(ServiceError) as exc_info:
        service.start_folder_preview(use_ai=False, confirm_external=False)
    assert exc_info.value.code == "folder_apply_recovery_required"
    assert service.operation()["state"] == "idle"


def test_folder_apply_delegates_journal_recovery_and_undo_wal_to_core(tmp_path, monkeypatch):
    from core import community_router

    calls = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def forbidden_reconcile(*_args, **_kwargs):
        raise AssertionError("service must not duplicate core recovery")

    def apply(storage, client, **kwargs):
        calls.append((storage, client, kwargs))
        return SimpleNamespace(
            planned_at=1700000200,
            plan_id="a" * 32,
            applied_count=1,
            public_rows=lambda: [{"file_id": "rec-1", "applied": True}],
        )

    monkeypatch.setattr(community_router, "reconcile_apply_journal", forbidden_reconcile)
    monkeypatch.setattr(community_router, "reconcile_undo_journal", forbidden_reconcile)
    monkeypatch.setattr(community_router, "apply_saved_plan", apply)
    storage = object()
    client = Client()
    service = CommunityService(
        _paths(tmp_path),
        storage_factory=lambda: storage,
        config_loader=lambda: object(),
        client_factory=lambda _config: client,
        auth_loader=_auth,
    )

    result = service._apply_folders(
        lambda *_args: None,
        file_ids=["rec-1"],
        plan_id="a" * 32,
    )

    assert result == "Plaud Cloud에서 1개를 이동했습니다. 실패 0개."
    assert calls == [
        (
            storage,
            client,
            {
                "selected_ids": ["rec-1"],
                "plan_path": tmp_path / "auto_folder_preview.json",
                "expected_plan_id": "a" * 32,
                "undo_path": tmp_path / "last_classify.json",
                "min_confidence": 0.6,
            },
        )
    ]


def test_folder_apply_recovery_clears_replayable_preview(tmp_path, monkeypatch):
    from core import community_router

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def recovered(*_args, **_kwargs):
        raise community_router.ApplyJournalError(
            "recovered an interrupted folder apply; review its undo record before retrying"
        )

    monkeypatch.setattr(community_router, "apply_saved_plan", recovered)
    service = CommunityService(
        _paths(tmp_path),
        storage_factory=object,
        config_loader=object,
        client_factory=lambda _config: Client(),
        auth_loader=_auth,
    )
    with service._state_lock:
        service._folder_preview_plan_id = "a" * 32
        service._folder_preview_phase = "preview"

    with pytest.raises(ServiceError) as exc_info:
        service._apply_folders(
            lambda *_args: None,
            file_ids=["rec-1"],
            plan_id="a" * 32,
        )

    assert exc_info.value.code == "folder_apply_recovered"
    assert "중단됐던 폴더 적용을 복구했습니다" in exc_info.value.message
    with service._state_lock:
        assert service._folder_preview_plan_id == ""
        assert service._folder_preview_phase == "recovered"


def test_folder_undo_delegates_recovery_and_manifest_updates_to_core(tmp_path, monkeypatch):
    from core import community_router

    calls = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def forbidden_reconcile(*_args, **_kwargs):
        raise AssertionError("service must not duplicate core recovery")

    def undo(storage, client, undo_path):
        calls.append((storage, client, undo_path))
        return SimpleNamespace(
            remaining_count=0,
            public_dict=lambda: {"status": "ok", "reverted": 1, "failed": []},
        )

    monkeypatch.setattr(community_router, "reconcile_apply_journal", forbidden_reconcile)
    monkeypatch.setattr(community_router, "reconcile_undo_journal", forbidden_reconcile)
    monkeypatch.setattr(community_router, "undo_saved_manifest", undo)
    storage = object()
    client = Client()
    service = CommunityService(
        _paths(tmp_path),
        storage_factory=lambda: storage,
        config_loader=lambda: object(),
        client_factory=lambda _config: client,
        auth_loader=_auth,
    )

    result = service._undo_folders(lambda *_args: None)

    assert result == "Plaud Cloud 폴더 이동 1개를 되돌렸습니다."
    assert calls == [(storage, client, tmp_path / "last_classify.json")]


def test_folder_undo_reports_apply_recovery_as_a_separate_first_phase(tmp_path, monkeypatch):
    from core import community_router

    detail = "중단된 폴더 적용을 안정화했습니다. 상태를 확인한 뒤 되돌리기를 다시 실행하세요."

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(
        community_router,
        "undo_saved_manifest",
        lambda *_args: SimpleNamespace(
            remaining_count=2,
            public_dict=lambda: {
                "status": "apply_recovery_required",
                "detail": detail,
                "reverted": 0,
                "failed": [],
                "apply_recovery_required": True,
            },
        ),
    )
    service = CommunityService(
        _paths(tmp_path),
        storage_factory=object,
        config_loader=object,
        client_factory=lambda _config: Client(),
        auth_loader=_auth,
    )
    with service._state_lock:
        service._folder_preview_rows = [{"file_id": "rec-1"}]
        service._folder_preview_plan_id = "a" * 32
        service._folder_preview_phase = "preview"

    result = service._undo_folders(lambda *_args: None)

    assert result == detail
    assert service.folder_preview()["phase"] == "recovered"


def test_folder_undo_rejects_malformed_manifest_before_cloud_access(tmp_path):
    storage, folders, _, manifest_path = _undo_fixture(tmp_path)
    malformed = json.loads(manifest_path.read_text())
    malformed["moved"][0]["previous_folder_ids"] = ["one", "two"]
    manifest_path.write_text(json.dumps(malformed), encoding="utf-8")

    client = UndoClient(
        folders,
        {"rec-1": ["folder-target"], "rec-2": ["folder-target"]},
    )

    service = CommunityService(
        _paths(tmp_path),
        storage_factory=lambda: storage,
        config_loader=lambda: object(),
        client_factory=lambda _config: client,
        auth_loader=_auth,
    )

    service.start_folder_undo(confirm_undo=True)
    result = _wait_for_job(service)

    assert result["state"] == "failed"
    assert "기록 형식이 올바르지 않아" in result["message"]
    assert client.detail_calls == []
    assert client.mutations == []


@pytest.mark.parametrize("conflict_side", ["local", "cloud"])
def test_folder_undo_preflights_every_item_before_first_patch(tmp_path, conflict_side):
    storage, folders, moved, manifest_path = _undo_fixture(tmp_path)
    remote = {"rec-1": ["folder-target"], "rec-2": ["folder-target"]}
    if conflict_side == "local":
        storage.set_file_folders("rec-2", [])
    else:
        remote["rec-2"] = ["folder-previous"]
    client = UndoClient(folders, remote)
    original_manifest = manifest_path.read_bytes()
    service = CommunityService(
        _paths(tmp_path),
        storage_factory=lambda: storage,
        config_loader=lambda: object(),
        client_factory=lambda _config: client,
        auth_loader=_auth,
    )

    service.start_folder_undo(confirm_undo=True)
    result = _wait_for_job(service)

    assert result["state"] == "failed"
    assert "아무 항목도 변경하지 않았습니다" in result["message"]
    if conflict_side == "local":
        assert client.detail_calls == []
    else:
        assert client.detail_calls == ["rec-1", "rec-2"]
    assert client.mutations == []
    assert manifest_path.read_bytes() == original_manifest
    assert json.loads(manifest_path.read_text())["moved"] == moved


def test_folder_undo_atomically_retains_only_cloud_failures(tmp_path):
    storage, folders, moved, manifest_path = _undo_fixture(tmp_path)
    client = UndoClient(
        folders,
        {"rec-1": ["folder-target"], "rec-2": ["folder-target"]},
        fail_ids={"rec-1"},
    )
    service = CommunityService(
        _paths(tmp_path),
        storage_factory=lambda: storage,
        config_loader=lambda: object(),
        client_factory=lambda _config: client,
        auth_loader=_auth,
    )

    service.start_folder_undo(confirm_undo=True)
    result = _wait_for_job(service)

    assert result["state"] == "succeeded"
    assert result["done"] == 2
    assert result["failed"] == 1
    assert client.mutations == [
        ("rec-1", []),
        ("rec-2", ["folder-previous"]),
    ]
    remaining = json.loads(manifest_path.read_text())
    assert remaining["schema_version"] == 2
    assert remaining["moved"] == [moved[0]]
    with storage._connect() as conn:
        local = conn.execute(
            "SELECT file_id, folder_id FROM file_folders ORDER BY file_id"
        ).fetchall()
    assert [tuple(row) for row in local] == [
        ("rec-1", "folder-target"),
        ("rec-2", "folder-previous"),
    ]
    assert service.folder_preview()["undo_available"] is True


def test_folder_undo_manifest_cas_detects_noncooperating_replacement(tmp_path):
    storage, folders, _, manifest_path = _undo_fixture(tmp_path)
    replacement = json.loads(manifest_path.read_text())
    replacement["moved"][0]["title"] = "다른 작업이 교체한 기록"
    replacement_bytes = json.dumps(replacement, ensure_ascii=False, indent=2).encode("utf-8")

    def replace_during_preflight(_file_id, call_count):
        if call_count == 2:
            manifest_path.write_bytes(replacement_bytes)

    client = UndoClient(
        folders,
        {"rec-1": ["folder-target"], "rec-2": ["folder-target"]},
        on_detail=replace_during_preflight,
    )
    service = CommunityService(
        _paths(tmp_path),
        storage_factory=lambda: storage,
        config_loader=lambda: object(),
        client_factory=lambda _config: client,
        auth_loader=_auth,
    )

    service.start_folder_undo(confirm_undo=True)
    result = _wait_for_job(service)

    assert result["state"] == "failed"
    assert "다른 작업에서 바뀌어" in result["message"]
    assert client.mutations == []
    assert manifest_path.read_bytes() == replacement_bytes
    assert json.loads(manifest_path.read_text())["moved"] == replacement["moved"]


def test_folder_apply_rejects_when_there_is_no_current_preview(tmp_path, monkeypatch):
    from core import app_config

    monkeypatch.setattr(app_config, "CONFIG_FILE", tmp_path / "config.json")
    service = CommunityService(
        _paths(tmp_path),
        storage_factory=lambda: _read_storage(tmp_path),
        auth_loader=_auth,
    )
    with pytest.raises(ServiceError) as exc_info:
        service.start_folder_apply(
            file_ids=["rec-1"],
            plan_id="a" * 32,
            confirm_apply=True,
        )
    assert exc_info.value.code == "preview_replaced"


def test_folder_apply_requires_exact_current_preview_plan_id(tmp_path, monkeypatch):
    from core import app_config

    monkeypatch.setattr(app_config, "CONFIG_FILE", tmp_path / "config.json")
    service = CommunityService(_paths(tmp_path), auth_loader=_auth)
    with service._state_lock:
        service._folder_preview_rows = [
            {"file_id": "rec-1", "folder_id": "folder-1", "confidence": 0.9}
        ]
        service._folder_preview_plan_id = "a" * 32
        service._folder_preview_phase = "preview"

    with pytest.raises(ServiceError) as exc_info:
        service.start_folder_apply(
            file_ids=["rec-1"],
            plan_id="b" * 32,
            confirm_apply=True,
        )

    assert exc_info.value.code == "preview_replaced"
    assert service.operation()["state"] == "idle"


def test_busy_preview_attempt_preserves_the_reviewed_plan(tmp_path, monkeypatch):
    from core import app_config

    monkeypatch.setattr(app_config, "CONFIG_FILE", tmp_path / "config.json")
    service = CommunityService(_paths(tmp_path), auth_loader=_auth)
    original_rows = [{"file_id": "rec-1", "folder_id": "folder-1", "confidence": 0.9}]
    with service._state_lock:
        service._folder_preview_rows = list(original_rows)
        service._folder_preview_planned_at = 1700000200
        service._folder_preview_plan_id = "a" * 32
        service._folder_preview_phase = "preview"

    assert service._job_gate.acquire(blocking=False)
    try:
        with pytest.raises(ServiceError) as exc_info:
            service.start_folder_preview(use_ai=False, confirm_external=False)
    finally:
        service._job_gate.release()

    assert exc_info.value.code == "busy"
    assert service.folder_preview() == {
        "items": original_rows,
        "planned_at": 1700000200,
        "plan_id": "a" * 32,
        "limit": 200,
        "phase": "preview",
        "undo_available": False,
        "apply_recovery_required": False,
    }


def test_folder_preview_snapshots_provider_backend_and_model_once(tmp_path, monkeypatch):
    from core import app_config, community_models, community_router

    captured = {}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_route(*args, **kwargs):
        captured.update(kwargs)
        return community_router.RoutingReport(
            planned_at=1700000200,
            plan_id="a" * 32,
            catalog_fingerprint="b" * 64,
        )

    monkeypatch.setattr(
        app_config,
        "load",
        lambda: {
            "classify_model": "codex",
            "backends": {"codex": "api"},
            "models": {"codex": "gpt-snapshot"},
        },
    )
    monkeypatch.setattr(community_models, "model_available", lambda *_args: True)
    monkeypatch.setattr(community_router, "route_recordings", fake_route)
    monkeypatch.setattr(
        community_router,
        "write_preview_plan",
        lambda report, _path: report.plan_id,
    )
    service = CommunityService(
        _paths(tmp_path),
        storage_factory=object,
        config_loader=object,
        client_factory=lambda _config: Client(),
        auth_loader=_auth,
    )

    service.start_folder_preview(use_ai=True, confirm_external=True)
    assert _wait_for_job(service)["state"] == "succeeded"

    assert captured["provider"] == "codex"
    assert captured["backend"] == "api"
    assert captured["model_id"] == "gpt-snapshot"


@pytest.mark.parametrize("provider", ["gemini", "grok"])
def test_api_only_cli_backend_is_rejected_in_settings(tmp_path, monkeypatch, provider):
    from core import app_config

    monkeypatch.setattr(app_config, "CONFIG_FILE", tmp_path / "config.json")
    service = CommunityService(_paths(tmp_path), auth_loader=_auth)
    with pytest.raises(ServiceError) as exc_info:
        service.set_routing_settings(provider, "cli", f"{provider}-test")
    assert exc_info.value.code == "invalid_backend"


@pytest.mark.parametrize(
    "usage_status",
    [None, 1, "", "drafted", "metadata-ready ", "METADATA-READY"],
)
def test_local_usage_status_rejects_values_outside_the_five_states(tmp_path, usage_status):
    service = CommunityService(
        _paths(tmp_path), storage_factory=lambda: _read_storage(tmp_path), auth_loader=_auth
    )
    with pytest.raises(ServiceError) as exc_info:
        service.set_usage_status("rec-1", usage_status)
    assert exc_info.value.code == "invalid_usage_status"


@pytest.mark.parametrize(
    "tag",
    [None, 1, "", "   ", "one,two", "bad\ntag", "가" * 65],
)
def test_local_tag_write_rejects_ambiguous_or_oversized_input(tmp_path, tag):
    storage = _read_storage(tmp_path)
    service = CommunityService(_paths(tmp_path), storage_factory=lambda: storage, auth_loader=_auth)
    with pytest.raises(ServiceError) as exc_info:
        service.add_tag("rec-1", tag)
    assert exc_info.value.code == "invalid_tag"
    assert storage.list_note_tags("rec-1") == []


@pytest.mark.parametrize("file_id", [None, 7, "", "../rec-1", "missing"])
def test_local_metadata_writes_require_an_active_known_recording(tmp_path, file_id):
    storage = _read_storage(tmp_path)
    service = CommunityService(_paths(tmp_path), storage_factory=lambda: storage, auth_loader=_auth)
    with pytest.raises(ServiceError) as exc_info:
        service.set_usage_status(file_id, "archived")
    expected = "not_found" if file_id == "missing" else "invalid_file_id"
    assert exc_info.value.code == expected


def test_local_metadata_writes_reject_trashed_recordings(tmp_path):
    storage = _read_storage(tmp_path)
    with storage._connect() as conn:
        conn.execute("UPDATE files SET is_trash = 1 WHERE id = 'rec-1'")
    service = CommunityService(_paths(tmp_path), storage_factory=lambda: storage, auth_loader=_auth)

    with pytest.raises(ServiceError) as exc_info:
        service.add_tag("rec-1", "private")
    assert exc_info.value.code == "not_found"
    assert exc_info.value.status == 404


class WriteStorage:
    def __init__(self):
        self.calls = []
        self.saved = []
        self.pending_calls = 0

    def counts(self):
        return {"total": 0, "trash": 0, "folders": 0, "cached": 0}

    def replace_folders(self, folders, *, now):
        self.calls.append(("replace_folders", len(folders)))

    def upsert_file(self, item, *, now, is_trash):
        self.calls.append(("upsert_file", item.id, is_trash))

    def files_without_content(self):
        self.pending_calls += 1
        return [{"id": "rec-1"}, {"id": "rec-2"}]

    def save_content(self, content, *, now):
        self.saved.append(content.file_id)


class ReadOnlyClient:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def list_folders(self):
        self.calls.append("list_folders")
        return [SimpleNamespace(id="folder-1")]

    def list_files(self, *, limit, skip, is_trash):
        self.calls.append(("list_files", is_trash, skip))
        return SimpleNamespace(total=1, items=[SimpleNamespace(id=f"rec-{is_trash}")])

    def file_content(self, file_id):
        self.calls.append(("file_content", file_id))
        return SimpleNamespace(file_id=file_id)

    def __getattr__(self, name):
        if name.startswith(("create", "rename", "delete", "set_", "update_", "sync_speakers")):
            raise AssertionError(f"cloud mutation method requested: {name}")
        raise AttributeError(name)


def _wait_for_job(service):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = service.operation()
        if state["state"] != "running":
            return state
        time.sleep(0.01)
    raise AssertionError("background job did not finish")


def test_status_never_starts_backfill_and_jobs_only_read_cloud(tmp_path):
    storage = WriteStorage()
    client = ReadOnlyClient()
    service = CommunityService(
        _paths(tmp_path),
        storage_factory=lambda: storage,
        config_loader=lambda: object(),
        client_factory=lambda config: client,
        auth_loader=_auth,
    )

    service.status()
    assert storage.pending_calls == 0
    assert client.calls == []

    service.start_sync()
    assert _wait_for_job(service)["state"] == "succeeded"
    assert client.calls == [
        "list_folders",
        ("list_files", 0, 0),
        ("list_files", 1, 0),
    ]

    client.calls.clear()
    service.start_backfill(limit=1)
    result = _wait_for_job(service)
    assert result["state"] == "succeeded"
    assert storage.pending_calls == 1
    assert storage.saved == ["rec-1"]
    assert client.calls == [("file_content", "rec-1")]


def test_sync_paginates_until_reported_total(tmp_path):
    storage = WriteStorage()

    class PaginatedClient(ReadOnlyClient):
        def list_files(self, *, limit, skip, is_trash):
            self.calls.append(("list_files", is_trash, skip))
            if is_trash:
                return SimpleNamespace(total=0, items=[])
            items = [SimpleNamespace(id=f"rec-{skip}")] if skip < 3 else []
            return SimpleNamespace(total=3, items=items)

    client = PaginatedClient()
    service = CommunityService(
        _paths(tmp_path),
        storage_factory=lambda: storage,
        config_loader=lambda: object(),
        client_factory=lambda config: client,
        auth_loader=_auth,
    )

    service.start_sync()
    assert _wait_for_job(service)["state"] == "succeeded"
    assert client.calls == [
        "list_folders",
        ("list_files", 0, 0),
        ("list_files", 0, 1),
        ("list_files", 0, 2),
        ("list_files", 1, 0),
    ]
    assert [call[1] for call in storage.calls if call[0] == "upsert_file"] == [
        "rec-0",
        "rec-1",
        "rec-2",
    ]


def test_shutdown_is_rejected_while_job_is_running(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingService(CommunityService):
        def _sync_metadata(self, progress):
            entered.set()
            if not release.wait(timeout=3):
                raise RuntimeError("test release timed out")
            return "완료"

    service = BlockingService(
        _paths(tmp_path),
        storage_factory=lambda: WriteStorage(),
        auth_loader=_auth,
    )
    service.start_sync()
    assert entered.wait(timeout=1)
    with pytest.raises(ServiceError) as exc_info:
        service.prepare_shutdown()
    assert exc_info.value.code == "busy"
    assert exc_info.value.status == 409

    release.set()
    assert _wait_for_job(service)["state"] == "succeeded"
    assert service.prepare_shutdown() == {"status": "shutting_down"}
    with pytest.raises(ServiceError) as exc_info:
        service.start_sync()
    assert exc_info.value.code == "shutting_down"


def test_curl_import_and_disconnect_do_not_return_credentials(tmp_path):
    refreshes = []
    disconnects = []
    secret = "bearer very-sensitive-value"

    def refresh(curl_text, path):
        refreshes.append((curl_text, path))
        return SimpleNamespace(status="ok", cookie_captured=True)

    service = CommunityService(
        _paths(tmp_path),
        storage_factory=lambda: WriteStorage(),
        auth_refresher=refresh,
        credential_disconnect=lambda path: disconnects.append(path),
    )

    curl_text = f"curl command containing {secret}"
    result = service.import_curl(curl_text)
    assert result == {
        "status": "connected",
        "verification": "verified",
        "cookie_captured": True,
    }
    assert secret not in json.dumps(result)
    assert refreshes == [(curl_text, tmp_path / "settings.env")]

    assert service.disconnect() == {"status": "disconnected"}
    assert disconnects == [tmp_path / "settings.env"]


def test_default_curl_import_requires_live_validation():
    source = inspect.getsource(CommunityService._default_auth_refresher)
    assert "validate_live=True" in source


def test_curl_live_rejection_and_unreachable_both_fail_closed(tmp_path):
    paths = _paths(tmp_path)
    rejected = CommunityService(
        paths,
        storage_factory=lambda: WriteStorage(),
        auth_refresher=lambda text, path: SimpleNamespace(
            status="live_auth_failed", cookie_captured=False
        ),
    )
    with pytest.raises(ServiceError) as exc_info:
        rejected.import_curl("curl with rejected credential")
    assert exc_info.value.code == "live_auth_failed"
    assert exc_info.value.status == 401

    unreachable = CommunityService(
        paths,
        storage_factory=lambda: WriteStorage(),
        auth_refresher=lambda text, path: SimpleNamespace(
            status="live_check_unavailable", cookie_captured=True
        ),
    )
    with pytest.raises(ServiceError) as exc_info:
        unreachable.import_curl("curl while network is unavailable")
    assert exc_info.value.code == "live_check_unavailable"
    assert exc_info.value.status == 503
    assert "저장하지 않았습니다" in exc_info.value.message
    assert "curl while" not in exc_info.value.message
