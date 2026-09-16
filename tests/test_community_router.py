"""Closed-vocabulary Community folder routing and explicit mutation gates."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import cli.main as cli_mod
import core.app_config as app_config_mod
import core.community_router as router_mod
import core.paths as paths_mod
from cli.main import app
from core.client import PlaudAPIError
from core.community_router import (
    ApplyJournalError,
    FolderCatalog,
    PreviewPlanError,
    RecordingSnapshot,
    RoutingReport,
    UndoReport,
    UndoJournalError,
    UndoManifestError,
    apply_journal_path,
    apply_saved_plan,
    classify_undo_status,
    reconcile_apply_journal,
    replace_undo_manifest,
    route_recordings,
    undo_journal_path,
    undo_saved_manifest,
    write_preview_plan,
    write_undo_manifest,
)
from core.models import FileContent, Folder, PlaudFile, SummaryBlock, TranscriptSegment
from core.storage import Storage


class FakeClient:
    def __init__(
        self,
        folders: list[Folder],
        remote_folders: dict[str, list[str]] | None = None,
        mutation_failures: set[str] | None = None,
    ) -> None:
        self._folders = folders
        self.remote_folders = {
            file_id: list(folder_ids) for file_id, folder_ids in (remote_folders or {}).items()
        }
        self.mutation_failures = set(mutation_failures or ())
        self.detail_calls: list[str] = []
        self.mutations: list[tuple[str, list[str]]] = []

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def list_folders(self) -> list[Folder]:
        return list(self._folders)

    def file_detail(self, file_id: str) -> dict:
        self.detail_calls.append(file_id)
        return {"data": {"filetag_id_list": list(self.remote_folders.get(file_id, []))}}

    def set_file_folders_once(self, file_id: str, folder_ids: list[str]) -> None:
        if file_id in self.mutation_failures:
            raise RuntimeError("injected Cloud failure")
        self.remote_folders[file_id] = list(folder_ids)
        self.mutations.append((file_id, list(folder_ids)))

    def set_file_folders(self, file_id: str, folder_ids: list[str]) -> None:
        """Compatibility alias for test setup outside the WAL path."""

        self.set_file_folders_once(file_id, folder_ids)


def _storage(tmp_path: Path, *titles: tuple[str, str]) -> Storage:
    storage = Storage(tmp_path / "router.db")
    for file_id, title in titles:
        storage.upsert_file(PlaudFile(id=file_id, filename=title), now=1)
        storage.save_content(
            FileContent(
                file_id=file_id,
                title=title,
                transcript=[
                    TranscriptSegment(start_time=0, end_time=1, content=f"{title} 상세 내용")
                ],
                summaries=[SummaryBlock(kind="auto_sum_note", body_md=f"{title} 요약")],
            ),
            now=1,
        )
    return storage


def test_no_existing_folders_returns_error_without_mutation(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    client = FakeClient([])

    report = route_recordings(storage, client)

    assert report.decisions == []
    assert "no Plaud folders" in report.error
    assert client.mutations == []


def test_duplicate_folder_name_is_ambiguous(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "고객 회의"))
    client = FakeClient([Folder(id="a", name="회의"), Folder(id="b", name="회의")])

    report = route_recordings(storage, client)

    assert report.decisions[0].folder_id == ""
    assert report.decisions[0].reason == "ambiguous duplicate folder name"
    assert client.mutations == []


def test_catalog_rejects_invented_and_ambiguous_names() -> None:
    catalog = FolderCatalog([Folder(id="a", name="회의"), Folder(id="b", name="회의")])
    with pytest.raises(ValueError, match="unknown existing folder"):
        catalog.resolve(folder_name="모델이 만든 폴더")
    with pytest.raises(ValueError, match="ambiguous existing folder"):
        catalog.resolve(folder_name="회의")
    assert catalog.resolve(folder_id="b").id == "b"


def test_malformed_model_output_falls_back_to_local_heuristic(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "주간 업데이트"))
    # A transcript-only token is deliberately below the LLM arbitration threshold.
    content = FileContent(
        file_id="f1",
        title="주간 업데이트",
        transcript=[TranscriptSegment(start_time=0, end_time=1, content="제품 회의")],
    )
    storage.save_content(content, now=2)
    client = FakeClient([Folder(id="meeting-id", name="회의")])
    calls: list[str] = []

    def malformed(*_args, **_kwargs) -> str:
        calls.append("called")
        return "not json"

    report = route_recordings(
        storage,
        client,
        use_llm=True,
        provider="claude",
        backend="cli",
        confirmed_external=True,
        llm_runner=malformed,
    )

    decision = report.decisions[0]
    assert calls == ["called"]
    assert decision.folder_id == "meeting-id"
    assert decision.source == "deterministic"
    assert "model fallback" in decision.error


def test_unavailable_model_falls_back_without_cloud_mutation(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "주간 업데이트"))
    storage.save_content(
        FileContent(
            file_id="f1",
            title="주간 업데이트",
            transcript=[TranscriptSegment(start_time=0, end_time=1, content="제품 회의")],
        ),
        now=2,
    )
    client = FakeClient([Folder(id="meeting-id", name="회의")])

    def unavailable(*_args, **_kwargs) -> str:
        raise RuntimeError("provider unavailable")

    report = route_recordings(
        storage,
        client,
        use_llm=True,
        provider="claude",
        backend="cli",
        confirmed_external=True,
        llm_runner=unavailable,
    )

    assert report.decisions[0].folder_id == "meeting-id"
    assert "model fallback" in report.decisions[0].error
    assert client.mutations == []


def test_unavailable_or_unconfirmed_model_never_runs_and_uses_local_fallback(
    tmp_path: Path,
) -> None:
    storage = _storage(tmp_path, ("f1", "주간 업데이트"))
    storage.save_content(
        FileContent(
            file_id="f1",
            title="주간 업데이트",
            transcript=[TranscriptSegment(start_time=0, end_time=1, content="제품 회의")],
        ),
        now=2,
    )
    client = FakeClient([Folder(id="meeting-id", name="회의")])

    def must_not_run(*_args, **_kwargs) -> str:
        raise AssertionError("external runner must stay closed")

    report = route_recordings(
        storage,
        client,
        use_llm=True,
        provider="claude",
        backend="cli",
        confirmed_external=False,
        llm_runner=must_not_run,
    )

    decision = report.decisions[0]
    assert decision.folder_id == "meeting-id"
    assert "confirm this preview" in decision.error
    assert client.mutations == []


def test_model_must_return_an_exact_existing_folder(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "분류가 필요한 기록"))
    client = FakeClient([Folder(id="real-id", name="연구")])

    def invented(*_args, **_kwargs) -> str:
        return json.dumps({"folder_id": "made-up", "folder_name": "새 폴더", "confidence": 0.99})

    report = route_recordings(
        storage,
        client,
        use_llm=True,
        provider="codex",
        backend="api",
        confirmed_external=True,
        llm_runner=invented,
    )

    assert report.decisions[0].folder_id == ""
    assert report.decisions[0].error == "model fallback: ValueError"
    assert client.mutations == []


def test_model_arbitration_obeys_per_preview_request_cap(tmp_path: Path) -> None:
    storage = _storage(
        tmp_path,
        ("f1", "분류 필요 1"),
        ("f2", "분류 필요 2"),
        ("f3", "분류 필요 3"),
    )
    client = FakeClient([Folder(id="research", name="연구")])
    calls: list[str] = []

    def choose(*_args, **_kwargs) -> str:
        calls.append("called")
        return json.dumps(
            {
                "folder_id": "research",
                "folder_name": "연구",
                "confidence": 0.8,
                "reason": "model match",
            }
        )

    report = route_recordings(
        storage,
        client,
        use_llm=True,
        provider="codex",
        backend="api",
        confirmed_external=True,
        llm_runner=choose,
        max_llm_calls=1,
    )

    assert calls == ["called"]
    assert sum(decision.source == "llm" for decision in report.decisions) == 1
    assert sum("request cap" in decision.error for decision in report.decisions) == 2
    assert client.mutations == []


def test_model_id_is_snapshotted_once_for_the_whole_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage(tmp_path, ("f1", "분류 필요 1"), ("f2", "분류 필요 2"))
    client = FakeClient([Folder(id="research", name="연구")])
    config_reads: list[str] = []
    runner_model_ids: list[str | None] = []

    def configured_model(provider: str) -> str:
        config_reads.append(provider)
        return "snapshot-model" if len(config_reads) == 1 else "changed-model"

    def choose(*_args, **kwargs) -> str:
        runner_model_ids.append(kwargs.get("model_id"))
        return json.dumps(
            {
                "folder_id": "research",
                "folder_name": "연구",
                "confidence": 0.8,
                "reason": "model match",
            }
        )

    monkeypatch.setattr(app_config_mod, "model_id_for", configured_model)

    route_recordings(
        storage,
        client,
        use_llm=True,
        provider="codex",
        backend="api",
        confirmed_external=True,
        llm_runner=choose,
    )

    assert config_reads == ["codex"]
    assert runner_model_ids == ["snapshot-model", "snapshot-model"]


def test_preview_never_mutates_and_apply_requires_explicit_selected_ids(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"), ("f2", "연구 자료"))
    client = FakeClient(
        [Folder(id="lecture-id", name="강의"), Folder(id="research-id", name="연구")]
    )

    preview = route_recordings(storage, client, selected_ids=["f1"])
    assert preview.decisions[0].folder_id == "lecture-id"
    assert client.mutations == []

    plan_path = tmp_path / "preview.json"
    write_preview_plan(preview, plan_path)
    with pytest.raises(PreviewPlanError, match="explicit selected"):
        apply_saved_plan(
            storage,
            client,
            selected_ids=[],
            plan_path=plan_path,
            expected_plan_id=preview.plan_id,
        )
    assert client.mutations == []


def test_apply_uses_only_exact_server_folder_id_for_selected_file(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"), ("f2", "연구 자료"))
    client = FakeClient(
        [Folder(id="lecture-id", name="강의"), Folder(id="research-id", name="연구")]
    )

    preview = route_recordings(storage, client)
    plan_path = tmp_path / "preview.json"
    write_preview_plan(preview, plan_path)
    report = apply_saved_plan(
        storage,
        client,
        selected_ids=["f1"],
        plan_path=plan_path,
        expected_plan_id=preview.plan_id,
        now=preview.planned_at,
    )

    assert report.applied_count == 1
    assert client.mutations == [("f1", ["lecture-id"])]
    with storage._connect() as conn:
        rows = conn.execute("SELECT file_id, folder_id FROM file_folders").fetchall()
    assert [(row["file_id"], row["folder_id"]) for row in rows] == [("f1", "lecture-id")]


def test_apply_preserves_previous_folder_for_exact_undo_manifest(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "강의 세미나"))
    folders = [Folder(id="old", name="프로젝트"), Folder(id="new", name="강의")]
    storage.replace_folders(folders, now=1)
    storage.set_file_folders("f1", ["old"])
    client = FakeClient(folders, {"f1": ["old"]})

    preview = route_recordings(
        storage,
        client,
        include_filed=True,
    )
    plan_path = tmp_path / "preview.json"
    write_preview_plan(preview, plan_path)
    report = apply_saved_plan(
        storage,
        client,
        selected_ids=["f1"],
        plan_path=plan_path,
        expected_plan_id=preview.plan_id,
        now=preview.planned_at,
    )

    assert report.applied_count == 1
    assert report.moved_manifest()[0]["previous_folder_ids"] == ["old"]


def test_apply_rejects_stale_or_changed_folder_catalog_before_cloud_write(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    original = FakeClient([Folder(id="lecture", name="강의")])
    preview = route_recordings(storage, original)
    plan_path = tmp_path / "preview.json"
    write_preview_plan(preview, plan_path)

    with pytest.raises(PreviewPlanError, match="stale"):
        apply_saved_plan(
            storage,
            original,
            selected_ids=["f1"],
            plan_path=plan_path,
            expected_plan_id=preview.plan_id,
            now=preview.planned_at + 1_801,
        )
    changed = FakeClient([Folder(id="lecture", name="강의"), Folder(id="new-folder", name="신규")])
    with pytest.raises(PreviewPlanError, match="catalog changed"):
        apply_saved_plan(
            storage,
            changed,
            selected_ids=["f1"],
            plan_path=plan_path,
            expected_plan_id=preview.plan_id,
            now=preview.planned_at,
        )
    assert original.mutations == []
    assert changed.mutations == []


def test_apply_rejects_replaced_preview_plan_before_cloud_write(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    client = FakeClient([Folder(id="lecture", name="강의")])
    reviewed = route_recordings(storage, client)
    plan_path = tmp_path / "preview.json"
    write_preview_plan(reviewed, plan_path)
    replacement = route_recordings(storage, client)
    write_preview_plan(replacement, plan_path)

    with pytest.raises(PreviewPlanError, match="replaced"):
        apply_saved_plan(
            storage,
            client,
            selected_ids=["f1"],
            plan_path=plan_path,
            expected_plan_id=reviewed.plan_id,
            now=reviewed.planned_at,
        )

    assert client.detail_calls == []
    assert client.mutations == []


def test_apply_preflights_all_remote_folder_states_before_first_patch(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"), ("f2", "연구 자료"))
    folders = [
        Folder(id="lecture", name="강의"),
        Folder(id="research", name="연구"),
        Folder(id="other", name="기타"),
    ]
    client = FakeClient(folders, {"f1": [], "f2": ["other"]})
    preview = route_recordings(storage, client)
    plan_path = tmp_path / "preview.json"
    write_preview_plan(preview, plan_path)

    with pytest.raises(PreviewPlanError, match="Plaud Cloud since preview"):
        apply_saved_plan(
            storage,
            client,
            selected_ids=["f1", "f2"],
            plan_path=plan_path,
            expected_plan_id=preview.plan_id,
            now=preview.planned_at,
        )

    assert client.detail_calls == ["f1", "f2"]
    assert client.mutations == []


def test_apply_rejects_recording_moved_since_preview(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    folders = [Folder(id="lecture", name="강의"), Folder(id="other", name="기타")]
    client = FakeClient(folders)
    preview = route_recordings(storage, client)
    plan_path = tmp_path / "preview.json"
    write_preview_plan(preview, plan_path)
    storage.replace_folders(folders, now=2)
    storage.set_file_folders("f1", ["other"])

    with pytest.raises(PreviewPlanError, match="changed since preview"):
        apply_saved_plan(
            storage,
            client,
            selected_ids=["f1"],
            plan_path=plan_path,
            expected_plan_id=preview.plan_id,
            now=preview.planned_at,
        )
    assert client.mutations == []


def test_apply_reuses_exact_llm_preview_without_second_model_call(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "분류 필요"))
    client = FakeClient([Folder(id="research", name="연구")])
    calls: list[str] = []

    def choose(*_args, **_kwargs) -> str:
        calls.append("called")
        return json.dumps(
            {
                "folder_id": "research",
                "folder_name": "연구",
                "confidence": 0.88,
                "reason": "approved preview",
            }
        )

    preview = route_recordings(
        storage,
        client,
        use_llm=True,
        provider="codex",
        backend="api",
        confirmed_external=True,
        llm_runner=choose,
    )
    plan_path = tmp_path / "preview.json"
    write_preview_plan(preview, plan_path)
    applied = apply_saved_plan(
        storage,
        client,
        selected_ids=["f1"],
        plan_path=plan_path,
        expected_plan_id=preview.plan_id,
        now=preview.planned_at,
    )

    assert calls == ["called"]
    assert applied.decisions[0].reason == "approved preview"
    assert client.mutations == [("f1", ["research"])]


def test_llm_preview_preserves_previous_folder_for_apply_and_undo(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "분류 필요"))
    folders = [Folder(id="old", name="보관"), Folder(id="new", name="연구")]
    storage.replace_folders(folders, now=1)
    storage.set_file_folders("f1", ["old"])
    client = FakeClient(folders, {"f1": ["old"]})

    preview = route_recordings(
        storage,
        client,
        include_filed=True,
        use_llm=True,
        provider="codex",
        backend="api",
        confirmed_external=True,
        llm_runner=lambda *_args, **_kwargs: json.dumps(
            {
                "folder_id": "new",
                "folder_name": "연구",
                "confidence": 0.88,
                "reason": "approved preview",
            }
        ),
    )
    assert preview.decisions[0].previous_folder_ids == ["old"]
    plan_path = tmp_path / "preview.json"
    write_preview_plan(preview, plan_path)

    applied = apply_saved_plan(
        storage,
        client,
        selected_ids=["f1"],
        plan_path=plan_path,
        expected_plan_id=preview.plan_id,
        now=preview.planned_at,
    )

    assert applied.applied_count == 1
    assert applied.moved_manifest()[0]["previous_folder_ids"] == ["old"]


def test_saved_preview_is_private_and_contains_no_recording_body(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    client = FakeClient([Folder(id="lecture", name="강의")])
    preview = route_recordings(storage, client)
    plan_path = tmp_path / "preview.json"

    saved_plan_id = write_preview_plan(preview, plan_path)

    document = plan_path.read_text(encoding="utf-8")
    assert saved_plan_id == preview.plan_id
    assert preview.public_rows()[0]["plan_id"] == preview.plan_id
    assert "상세 내용" not in document
    assert '"transcript":' not in document
    if os.name == "posix":
        assert plan_path.stat().st_mode & 0o777 == 0o600


def test_private_json_temp_name_uses_a_fresh_random_nonce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nonces = iter(("first-nonce", "second-nonce"))
    replaced_from: list[str] = []
    real_replace = router_mod.os.replace

    monkeypatch.setattr(router_mod.secrets, "token_hex", lambda _size: next(nonces))

    def tracked_replace(source: Path, destination: Path) -> None:
        replaced_from.append(Path(source).name)
        real_replace(source, destination)

    monkeypatch.setattr(router_mod.os, "replace", tracked_replace)
    report = RoutingReport(
        catalog_fingerprint="0" * 64,
        planned_at=1,
        plan_id="0" * 32,
    )
    path = tmp_path / "preview.json"

    write_preview_plan(report, path)
    write_preview_plan(report, path)

    assert replaced_from[0].endswith("-first-nonce")
    assert replaced_from[1].endswith("-second-nonce")
    assert replaced_from[0] != replaced_from[1]


def test_core_undo_manifest_writer_keeps_exact_previous_folders_private(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "강의 세미나"))
    folders = [Folder(id="old", name="프로젝트"), Folder(id="new", name="강의")]
    storage.replace_folders(folders, now=1)
    storage.set_file_folders("f1", ["old"])
    client = FakeClient(folders, {"f1": ["old"]})
    preview = route_recordings(storage, client, include_filed=True)
    plan_path = tmp_path / "preview.json"
    write_preview_plan(preview, plan_path)
    report = apply_saved_plan(
        storage,
        client,
        selected_ids=["f1"],
        plan_path=plan_path,
        expected_plan_id=preview.plan_id,
        now=preview.planned_at,
    )
    manifest_path = tmp_path / "undo.json"

    assert write_undo_manifest(report, manifest_path) == 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["moved"][0]["previous_folder_ids"] == ["old"]
    if os.name == "posix":
        assert manifest_path.stat().st_mode & 0o777 == 0o600


def test_cli_preview_then_apply_uses_saved_json_array_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    client = FakeClient([Folder(id="lecture-id", name="강의")])
    monkeypatch.setattr(paths_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli_mod, "Storage", lambda: storage)
    monkeypatch.setattr(cli_mod, "PlaudClient", lambda _cfg: client)
    monkeypatch.setattr(cli_mod, "load_config", lambda: object())
    runner = CliRunner()

    preview = runner.invoke(app, ["auto-folder", "--json"])
    assert preview.exit_code == 0, preview.output
    rows = json.loads(preview.stdout)
    assert isinstance(rows, list)
    assert rows[0]["file_id"] == "f1"
    assert rows[0]["folder_name"] == "강의"
    assert client.mutations == []
    assert (tmp_path / "auto_folder_preview.json").exists()

    applied = runner.invoke(
        app,
        [
            "auto-folder",
            "--apply",
            "--plan-id",
            rows[0]["plan_id"],
            "--only",
            "f1",
            "--json",
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.stdout)[0]["applied"] is True
    assert client.mutations == [("f1", ["lecture-id"])]
    assert (tmp_path / "last_classify.json").exists()


def test_cli_apply_requires_reviewed_plan_id_before_cloud_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    client = FakeClient([Folder(id="lecture-id", name="강의")])
    monkeypatch.setattr(paths_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli_mod, "Storage", lambda: storage)
    monkeypatch.setattr(cli_mod, "PlaudClient", lambda _cfg: client)
    monkeypatch.setattr(cli_mod, "load_config", lambda: object())

    result = CliRunner().invoke(app, ["auto-folder", "--apply", "--only", "f1", "--json"])

    assert result.exit_code != 0
    assert "--plan-id" in result.output
    assert client.mutations == []


@pytest.mark.parametrize("provider", ["gemini", "grok"])
def test_cli_rejects_api_only_provider_with_cli_backend(provider: str) -> None:
    result = CliRunner().invoke(
        app,
        [
            "auto-folder",
            "--llm",
            "--provider",
            provider,
            "--backend",
            "cli",
            "--confirm-external",
        ],
    )

    assert result.exit_code != 0
    assert "api backend only" in result.output

    config_result = CliRunner().invoke(app, ["config-backend", provider, "cli"])
    assert config_result.exit_code != 0
    assert "api backend only" in config_result.output


def test_classify_undo_restores_previous_exact_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 프로젝트"))
    folders = [Folder(id="old", name="프로젝트"), Folder(id="new", name="강의")]
    storage.replace_folders(folders, now=1)
    storage.set_file_folders("f1", ["new"])
    storage.upsert_note_metadata(file_id="f1", folder_id="new", folder_name="강의", now=1)
    client = FakeClient(folders, {"f1": ["new"]})
    manifest = tmp_path / "last_classify.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "moved": [
                    {
                        "file_id": "f1",
                        "folder_id": "new",
                        "folder_name": "강의",
                        "previous_folder_ids": ["old"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli_mod, "Storage", lambda: storage)
    monkeypatch.setattr(cli_mod, "PlaudClient", lambda _cfg: client)
    monkeypatch.setattr(cli_mod, "load_config", lambda: object())

    result = CliRunner().invoke(app, ["classify-undo", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "ok"
    assert client.mutations == [("f1", ["old"])]
    with storage._connect() as conn:
        folder_id = conn.execute(
            "SELECT folder_id FROM file_folders WHERE file_id = 'f1'"
        ).fetchone()[0]
    assert folder_id == "old"
    assert storage.get_note_metadata("f1")["folder_id"] == "old"
    assert not manifest.exists()


def test_classify_undo_json_exposes_apply_recovery_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 프로젝트"))
    client = FakeClient([Folder(id="new", name="강의")], {"f1": ["new"]})
    monkeypatch.setattr(paths_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli_mod, "Storage", lambda: storage)
    monkeypatch.setattr(cli_mod, "PlaudClient", lambda _cfg: client)
    monkeypatch.setattr(cli_mod, "load_config", lambda: object())
    monkeypatch.setattr(
        router_mod,
        "undo_saved_manifest",
        lambda *_args, **_kwargs: UndoReport(
            remaining_count=1,
            apply_recovery_required=True,
        ),
    )

    result = CliRunner().invoke(app, ["classify-undo", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "apply_recovery_required"
    assert payload["apply_recovery_required"] is True
    assert payload["reverted"] == 0


def test_classify_undo_status_is_read_only_and_counts_exact_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "last_classify.json"

    empty = classify_undo_status(manifest)

    assert empty.public_dict() == {
        "status": "none",
        "count": 0,
        "detail": "no folder undo artifacts found",
    }
    replace_undo_manifest(
        [
            {
                "file_id": "f1",
                "folder_id": "new",
                "folder_name": "강의",
                "title": "강의 준비",
                "previous_folder_ids": [],
            }
        ],
        manifest,
    )
    prior = manifest.read_bytes()

    available = classify_undo_status(manifest)

    assert available.status == "undo_available"
    assert available.count == 1
    assert manifest.read_bytes() == prior


def test_classify_undo_status_reports_apply_wal_without_network_or_mutation(
    tmp_path: Path,
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))

    class TimeoutClient(FakeClient):
        def set_file_folders_once(self, file_id: str, folder_ids: list[str]) -> None:
            raise TimeoutError("leave active apply WAL")

    client = TimeoutClient([Folder(id="lecture", name="강의")], {"f1": []})
    preview = route_recordings(storage, client)
    plan_path = tmp_path / "preview.json"
    manifest = tmp_path / "last_classify.json"
    write_preview_plan(preview, plan_path)
    with pytest.raises(ApplyJournalError):
        apply_saved_plan(
            storage,
            client,
            selected_ids=["f1"],
            plan_path=plan_path,
            expected_plan_id=preview.plan_id,
            undo_path=manifest,
            now=preview.planned_at,
        )
    detail_calls = list(client.detail_calls)
    journal_bytes = apply_journal_path(manifest).read_bytes()

    status = classify_undo_status(manifest)

    assert status.status == "apply_recovery_required"
    assert status.count == 1
    assert client.detail_calls == detail_calls
    assert client.mutations == []
    assert apply_journal_path(manifest).read_bytes() == journal_bytes


def test_classify_undo_status_cli_fails_closed_without_leaking_malformed_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_marker = "SECRET-RECORDING-TITLE"
    (tmp_path / "last_classify.json").write_text(secret_marker, encoding="utf-8")
    monkeypatch.setattr(paths_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        cli_mod,
        "PlaudClient",
        lambda *_args, **_kwargs: pytest.fail("status command attempted network setup"),
    )

    result = CliRunner().invoke(app, ["classify-undo-status", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload == {
        "status": "error",
        "count": 0,
        "detail": "folder undo artifacts are invalid or inconsistent",
    }
    assert secret_marker not in result.output


def test_classify_undo_rejects_any_malformed_entry_before_cloud_read_or_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage(tmp_path, ("f1", "첫째"), ("f2", "둘째"))
    folders = [Folder(id="old", name="이전"), Folder(id="new", name="현재")]
    client = FakeClient(folders, {"f1": ["new"], "f2": ["new"]})
    manifest = tmp_path / "last_classify.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "moved": [
                    {
                        "file_id": "f1",
                        "folder_id": "new",
                        "previous_folder_ids": ["old"],
                    },
                    {
                        "file_id": "f2",
                        "previous_folder_ids": ["old"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    original = manifest.read_bytes()
    monkeypatch.setattr(paths_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli_mod, "Storage", lambda: storage)
    monkeypatch.setattr(cli_mod, "PlaudClient", lambda _cfg: client)
    monkeypatch.setattr(cli_mod, "load_config", lambda: object())

    result = CliRunner().invoke(app, ["classify-undo", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "error"
    assert client.detail_calls == []
    assert client.mutations == []
    assert manifest.read_bytes() == original


def test_classify_undo_preflights_every_remote_target_before_first_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage(tmp_path, ("f1", "첫째"), ("f2", "둘째"))
    folders = [
        Folder(id="old", name="이전"),
        Folder(id="new", name="현재"),
        Folder(id="other", name="다른 폴더"),
    ]
    storage.replace_folders(folders, now=1)
    for file_id in ("f1", "f2"):
        storage.set_file_folders(file_id, ["new"])
    client = FakeClient(folders, {"f1": ["new"], "f2": ["other"]})
    manifest = tmp_path / "last_classify.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "moved": [
                    {
                        "file_id": file_id,
                        "folder_id": "new",
                        "previous_folder_ids": ["old"],
                    }
                    for file_id in ("f1", "f2")
                ],
            }
        ),
        encoding="utf-8",
    )
    original = manifest.read_bytes()
    monkeypatch.setattr(paths_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli_mod, "Storage", lambda: storage)
    monkeypatch.setattr(cli_mod, "PlaudClient", lambda _cfg: client)
    monkeypatch.setattr(cli_mod, "load_config", lambda: object())

    result = CliRunner().invoke(app, ["classify-undo", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert "changed in Plaud Cloud" in payload["detail"]
    assert client.detail_calls == ["f1", "f2"]
    assert client.mutations == []
    assert manifest.read_bytes() == original


def test_classify_undo_keeps_only_network_failures_in_locked_retry_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage(tmp_path, ("f1", "첫째"), ("f2", "둘째"))
    folders = [Folder(id="old", name="이전"), Folder(id="new", name="현재")]
    storage.replace_folders(folders, now=1)
    for file_id in ("f1", "f2"):
        storage.set_file_folders(file_id, ["new"])
    client = FakeClient(
        folders,
        {"f1": ["new"], "f2": ["new"]},
        mutation_failures={"f2"},
    )
    manifest = tmp_path / "last_classify.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "moved": [
                    {
                        "file_id": file_id,
                        "folder_id": "new",
                        "previous_folder_ids": ["old"],
                    }
                    for file_id in ("f1", "f2")
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli_mod, "Storage", lambda: storage)
    monkeypatch.setattr(cli_mod, "PlaudClient", lambda _cfg: client)
    monkeypatch.setattr(cli_mod, "load_config", lambda: object())

    result = CliRunner().invoke(app, ["classify-undo", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "partial"
    assert payload["reverted"] == 1
    assert payload["failed"] == [{"file_id": "f2", "error": "RuntimeError"}]
    assert client.detail_calls == ["f1", "f2", "f1", "f2", "f2"]
    assert client.mutations == [("f1", ["old"])]
    remaining = json.loads(manifest.read_text(encoding="utf-8"))["moved"]
    assert [entry["file_id"] for entry in remaining] == ["f2"]
    assert (tmp_path / ".last_classify.json.lock").is_file()


def test_apply_wal_write_failure_happens_before_first_cloud_patch_and_keeps_prior_undo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    client = FakeClient([Folder(id="lecture", name="강의")], {"f1": []})
    preview = route_recordings(storage, client)
    plan_path = tmp_path / "preview.json"
    undo_path = tmp_path / "last_classify.json"
    write_preview_plan(preview, plan_path)
    replace_undo_manifest(
        [
            {
                "file_id": "older",
                "folder_id": "lecture",
                "folder_name": "강의",
                "title": "이전 실행",
                "previous_folder_ids": [],
            }
        ],
        undo_path,
    )
    prior = undo_path.read_bytes()
    monkeypatch.setattr(
        router_mod,
        "_write_apply_journal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(ApplyJournalError, match="durably prepare"):
        apply_saved_plan(
            storage,
            client,
            selected_ids=["f1"],
            plan_path=plan_path,
            expected_plan_id=preview.plan_id,
            undo_path=undo_path,
            now=preview.planned_at,
        )

    assert client.mutations == []
    assert undo_path.read_bytes() == prior


def test_apply_recovers_patch_completed_before_applied_state_was_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    client = FakeClient([Folder(id="lecture", name="강의")], {"f1": []})
    preview = route_recordings(storage, client)
    plan_path = tmp_path / "preview.json"
    undo_path = tmp_path / "last_classify.json"
    write_preview_plan(preview, plan_path)
    real_write = router_mod._write_apply_journal
    failed = False

    def fail_first_applied(path, journal):
        nonlocal failed
        if not failed and journal["entries"][0]["state"] == "applied":
            failed = True
            raise OSError("simulated crash before applied journal fsync")
        real_write(path, journal)

    monkeypatch.setattr(router_mod, "_write_apply_journal", fail_first_applied)

    with pytest.raises(ApplyJournalError, match="needs recovery"):
        apply_saved_plan(
            storage,
            client,
            selected_ids=["f1"],
            plan_path=plan_path,
            expected_plan_id=preview.plan_id,
            undo_path=undo_path,
            now=preview.planned_at,
        )

    assert client.remote_folders["f1"] == ["lecture"]
    assert json.loads(apply_journal_path(undo_path).read_text())["entries"][0]["state"] == (
        "attempting"
    )
    recovery = reconcile_apply_journal(storage, client, undo_path)
    assert recovery.applied_count == 1
    assert client.mutations == [("f1", ["lecture"])]
    assert json.loads(undo_path.read_text())["moved"][0]["previous_folder_ids"] == []
    assert not apply_journal_path(undo_path).exists()


def test_apply_recovers_when_final_undo_manifest_write_failed_after_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    client = FakeClient([Folder(id="lecture", name="강의")], {"f1": []})
    preview = route_recordings(storage, client)
    plan_path = tmp_path / "preview.json"
    undo_path = tmp_path / "last_classify.json"
    write_preview_plan(preview, plan_path)
    real_replace = router_mod.replace_undo_manifest
    failed = False

    def fail_once(moved, path, *, lock_held=False):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("disk full")
        return real_replace(moved, path, lock_held=lock_held)

    monkeypatch.setattr(router_mod, "replace_undo_manifest", fail_once)

    with pytest.raises(ApplyJournalError, match="commit the undo record"):
        apply_saved_plan(
            storage,
            client,
            selected_ids=["f1"],
            plan_path=plan_path,
            expected_plan_id=preview.plan_id,
            undo_path=undo_path,
            now=preview.planned_at,
        )

    assert client.remote_folders["f1"] == ["lecture"]
    assert apply_journal_path(undo_path).exists()
    recovery = reconcile_apply_journal(storage, client, undo_path)
    assert recovery.applied_count == 1
    assert undo_path.exists()
    assert client.mutations == [("f1", ["lecture"])]


def test_apply_ambiguous_timeout_requires_exact_plan_before_resuming_patch(
    tmp_path: Path,
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))

    class TimeoutThenSuccess(FakeClient):
        timeout_once = True

        def set_file_folders_once(self, file_id: str, folder_ids: list[str]) -> None:
            if self.timeout_once:
                self.timeout_once = False
                raise TimeoutError("ambiguous transport timeout")
            super().set_file_folders_once(file_id, folder_ids)

    client = TimeoutThenSuccess([Folder(id="lecture", name="강의")], {"f1": []})
    preview = route_recordings(storage, client)
    plan_path = tmp_path / "preview.json"
    undo_path = tmp_path / "last_classify.json"
    write_preview_plan(preview, plan_path)

    with pytest.raises(ApplyJournalError, match="uncertain"):
        apply_saved_plan(
            storage,
            client,
            selected_ids=["f1"],
            plan_path=plan_path,
            expected_plan_id=preview.plan_id,
            undo_path=undo_path,
            now=preview.planned_at,
        )

    with pytest.raises(ApplyJournalError, match="different preview"):
        reconcile_apply_journal(
            storage,
            client,
            undo_path,
            resume_cloud=True,
            expected_plan_id="f" * 32,
        )
    recovery = reconcile_apply_journal(
        storage,
        client,
        undo_path,
        resume_cloud=True,
        expected_plan_id=preview.plan_id,
    )
    assert recovery.applied_count == 1
    assert client.mutations == [("f1", ["lecture"])]
    assert undo_path.exists()


@pytest.mark.parametrize("status_code", [None, 504])
def test_apply_wrapped_plaud_ambiguous_error_keeps_attempting_wal(
    tmp_path: Path,
    status_code: int | None,
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))

    class WrappedNetworkFailure(FakeClient):
        def set_file_folders_once(self, file_id: str, folder_ids: list[str]) -> None:
            raise PlaudAPIError("wrapped network failure", status_code=status_code)

    client = WrappedNetworkFailure([Folder(id="lecture", name="강의")], {"f1": []})
    preview = route_recordings(storage, client)
    plan_path = tmp_path / "preview.json"
    undo_path = tmp_path / "last_classify.json"
    write_preview_plan(preview, plan_path)

    with pytest.raises(ApplyJournalError, match="uncertain"):
        apply_saved_plan(
            storage,
            client,
            selected_ids=["f1"],
            plan_path=plan_path,
            expected_plan_id=preview.plan_id,
            undo_path=undo_path,
            now=preview.planned_at,
        )

    journal = json.loads(apply_journal_path(undo_path).read_text())
    assert journal["entries"][0]["state"] == "attempting"
    assert not undo_path.exists()


@pytest.mark.parametrize("remaining_ids", [("f2",), ()])
def test_committed_apply_journal_cleanup_never_resurrects_undo_reduced_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remaining_ids: tuple[str, ...],
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 첫째"), ("f2", "강의 둘째"))
    folders = [Folder(id="old", name="이전"), Folder(id="lecture", name="강의")]
    storage.replace_folders(folders, now=1)
    for file_id in ("f1", "f2"):
        storage.set_file_folders(file_id, ["old"])
    client = FakeClient(folders, {"f1": ["old"], "f2": ["old"]})
    preview = route_recordings(storage, client, include_filed=True)
    plan_path = tmp_path / "preview.json"
    undo_path = tmp_path / "last_classify.json"
    write_preview_plan(preview, plan_path)
    real_remove = router_mod._remove_private_file
    fail_cleanup = True

    def fail_terminal_cleanup(path: Path) -> None:
        if fail_cleanup and path == apply_journal_path(undo_path):
            raise OSError("simulated unlink failure")
        real_remove(path)

    monkeypatch.setattr(router_mod, "_remove_private_file", fail_terminal_cleanup)

    with pytest.raises(ApplyJournalError, match="terminal journal"):
        apply_saved_plan(
            storage,
            client,
            selected_ids=["f1", "f2"],
            plan_path=plan_path,
            expected_plan_id=preview.plan_id,
            undo_path=undo_path,
            now=preview.planned_at,
        )

    assert json.loads(apply_journal_path(undo_path).read_text())["phase"] == "committed"
    original_entries = json.loads(undo_path.read_text())["moved"]
    removed_ids = {"f1", "f2"} - set(remaining_ids)
    for file_id in sorted(removed_ids):
        client.set_file_folders(file_id, ["old"])
        storage.set_file_folders(file_id, ["old"])
    remaining = [entry for entry in original_entries if entry["file_id"] in remaining_ids]
    replace_undo_manifest(remaining, undo_path)
    reduced_bytes = undo_path.read_bytes() if remaining else None
    fail_cleanup = False

    recovery = reconcile_apply_journal(storage, client, undo_path)

    assert recovery.applied_count == 0
    assert not apply_journal_path(undo_path).exists()
    if remaining:
        assert undo_path.read_bytes() == reduced_bytes
    else:
        assert not undo_path.exists()

    undo = undo_saved_manifest(storage, client, undo_path)
    assert undo.reverted_count == len(remaining_ids)
    for file_id in removed_ids:
        assert client.mutations.count((file_id, ["old"])) == 1


def test_apply_partial_patch_publishes_only_exact_successes_to_undo(
    tmp_path: Path,
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"), ("f2", "연구 자료"))
    folders = [Folder(id="lecture", name="강의"), Folder(id="research", name="연구")]
    client = FakeClient(folders, {"f1": [], "f2": []}, mutation_failures={"f2"})
    preview = route_recordings(storage, client)
    plan_path = tmp_path / "preview.json"
    undo_path = tmp_path / "last_classify.json"
    write_preview_plan(preview, plan_path)

    report = apply_saved_plan(
        storage,
        client,
        selected_ids=["f1", "f2"],
        plan_path=plan_path,
        expected_plan_id=preview.plan_id,
        undo_path=undo_path,
        now=preview.planned_at,
    )

    assert report.applied_count == 1
    assert client.mutations == [("f1", ["lecture"])]
    moved = json.loads(undo_path.read_text())["moved"]
    assert [entry["file_id"] for entry in moved] == ["f1"]
    assert not apply_journal_path(undo_path).exists()


def test_apply_zero_success_does_not_replace_prior_undo_manifest(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    client = FakeClient(
        [Folder(id="lecture", name="강의")],
        {"f1": []},
        mutation_failures={"f1"},
    )
    preview = route_recordings(storage, client)
    plan_path = tmp_path / "preview.json"
    undo_path = tmp_path / "last_classify.json"
    write_preview_plan(preview, plan_path)
    replace_undo_manifest(
        [
            {
                "file_id": "older",
                "folder_id": "lecture",
                "folder_name": "강의",
                "title": "이전 실행",
                "previous_folder_ids": [],
            }
        ],
        undo_path,
    )
    prior = undo_path.read_bytes()

    report = apply_saved_plan(
        storage,
        client,
        selected_ids=["f1"],
        plan_path=plan_path,
        expected_plan_id=preview.plan_id,
        undo_path=undo_path,
        now=preview.planned_at,
    )

    assert report.applied_count == 0
    assert undo_path.read_bytes() == prior
    assert not apply_journal_path(undo_path).exists()


def test_apply_rechecks_each_remote_folder_immediately_before_its_patch(
    tmp_path: Path,
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"), ("f2", "연구 자료"))
    folders = [
        Folder(id="lecture", name="강의"),
        Folder(id="research", name="연구"),
        Folder(id="other", name="기타"),
    ]

    class ChangedBetweenPreflightAndPatch(FakeClient):
        def file_detail(self, file_id: str) -> dict:
            if file_id == "f2" and self.detail_calls.count("f2") == 1:
                self.remote_folders["f2"] = ["other"]
            return super().file_detail(file_id)

    client = ChangedBetweenPreflightAndPatch(folders, {"f1": [], "f2": []})
    preview = route_recordings(storage, client)
    plan_path = tmp_path / "preview.json"
    undo_path = tmp_path / "last_classify.json"
    write_preview_plan(preview, plan_path)

    report = apply_saved_plan(
        storage,
        client,
        selected_ids=["f1", "f2"],
        plan_path=plan_path,
        expected_plan_id=preview.plan_id,
        undo_path=undo_path,
        now=preview.planned_at,
    )

    assert report.applied_count == 1
    assert client.mutations == [("f1", ["lecture"])]
    assert report.decisions[1].error == "Cloud apply skipped: folder changed after preflight"
    assert [entry["file_id"] for entry in json.loads(undo_path.read_text())["moved"]] == ["f1"]


def test_router_never_calls_the_legacy_retrying_folder_patch(tmp_path: Path) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))

    class LegacyRetryTrap(FakeClient):
        def set_file_folders(self, file_id: str, folder_ids: list[str]) -> None:
            pytest.fail("router called the legacy retrying folder PATCH")

    client = LegacyRetryTrap([Folder(id="lecture", name="강의")], {"f1": []})
    preview = route_recordings(storage, client)
    plan_path = tmp_path / "preview.json"
    undo_path = tmp_path / "last_classify.json"
    write_preview_plan(preview, plan_path)

    report = apply_saved_plan(
        storage,
        client,
        selected_ids=["f1"],
        plan_path=plan_path,
        expected_plan_id=preview.plan_id,
        undo_path=undo_path,
        now=preview.planned_at,
    )

    assert report.applied_count == 1
    assert client.mutations == [("f1", ["lecture"])]


def test_interrupted_apply_is_stabilized_before_a_separate_explicit_undo(
    tmp_path: Path,
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))

    class TimeoutThenSuccess(FakeClient):
        attempts = 0

        def set_file_folders_once(self, file_id: str, folder_ids: list[str]) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise TimeoutError("original apply may still arrive late")
            super().set_file_folders_once(file_id, folder_ids)

    client = TimeoutThenSuccess([Folder(id="lecture", name="강의")], {"f1": []})
    preview = route_recordings(storage, client)
    plan_path = tmp_path / "preview.json"
    undo_path = tmp_path / "last_classify.json"
    write_preview_plan(preview, plan_path)

    with pytest.raises(ApplyJournalError, match="uncertain"):
        apply_saved_plan(
            storage,
            client,
            selected_ids=["f1"],
            plan_path=plan_path,
            expected_plan_id=preview.plan_id,
            undo_path=undo_path,
            now=preview.planned_at,
        )

    stabilized = undo_saved_manifest(storage, client, undo_path)

    assert stabilized.apply_recovery_required is True
    assert stabilized.reverted_count == 0
    assert stabilized.public_dict()["status"] == "apply_recovery_required"
    assert client.remote_folders["f1"] == ["lecture"]
    assert client.mutations == [("f1", ["lecture"])]
    assert undo_path.exists()

    undone = undo_saved_manifest(storage, client, undo_path)

    assert undone.apply_recovery_required is False
    assert undone.reverted_count == 1
    assert client.remote_folders["f1"] == []
    assert client.mutations == [("f1", ["lecture"]), ("f1", [])]


def test_undo_wal_write_failure_prevents_cloud_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    folders = [Folder(id="old", name="이전"), Folder(id="new", name="강의")]
    storage.replace_folders(folders, now=1)
    storage.set_file_folders("f1", ["new"])
    client = FakeClient(folders, {"f1": ["new"]})
    undo_path = tmp_path / "last_classify.json"
    replace_undo_manifest(
        [
            {
                "file_id": "f1",
                "folder_id": "new",
                "folder_name": "강의",
                "title": "강의 준비",
                "previous_folder_ids": ["old"],
            }
        ],
        undo_path,
    )
    prior = undo_path.read_bytes()
    monkeypatch.setattr(
        router_mod,
        "_write_undo_journal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(UndoJournalError, match="durably prepare"):
        undo_saved_manifest(storage, client, undo_path)

    assert client.mutations == []
    assert undo_path.read_bytes() == prior


def test_undo_rejects_any_local_state_mismatch_before_remote_preflight_or_patch(
    tmp_path: Path,
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    folders = [
        Folder(id="old", name="이전"),
        Folder(id="new", name="강의"),
        Folder(id="other", name="사용자 이동"),
    ]
    storage.replace_folders(folders, now=1)
    storage.set_file_folders("f1", ["other"])
    client = FakeClient(folders, {"f1": ["new"]})
    undo_path = tmp_path / "last_classify.json"
    replace_undo_manifest(
        [
            {
                "file_id": "f1",
                "folder_id": "new",
                "folder_name": "강의",
                "title": "강의 준비",
                "previous_folder_ids": ["old"],
            }
        ],
        undo_path,
    )

    with pytest.raises(UndoManifestError, match="changed locally"):
        undo_saved_manifest(storage, client, undo_path)

    assert client.detail_calls == []
    assert client.mutations == []


def test_undo_exact_manifest_cas_detects_replacement_during_remote_preflight(
    tmp_path: Path,
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    folders = [Folder(id="old", name="이전"), Folder(id="new", name="강의")]
    storage.replace_folders(folders, now=1)
    storage.set_file_folders("f1", ["new"])
    undo_path = tmp_path / "last_classify.json"
    replace_undo_manifest(
        [
            {
                "file_id": "f1",
                "folder_id": "new",
                "folder_name": "강의",
                "title": "강의 준비",
                "previous_folder_ids": ["old"],
            }
        ],
        undo_path,
    )

    class ReplacingClient(FakeClient):
        def file_detail(self, file_id: str) -> dict:
            if not self.detail_calls:
                document = json.loads(undo_path.read_text())
                document["at"] += 1
                undo_path.write_text(json.dumps(document), encoding="utf-8")
            return super().file_detail(file_id)

    client = ReplacingClient(folders, {"f1": ["new"]})

    with pytest.raises(UndoJournalError, match="replaced"):
        undo_saved_manifest(storage, client, undo_path)

    assert client.mutations == []
    assert client.remote_folders["f1"] == ["new"]


def test_undo_rejects_noncooperating_manifest_replacement_before_next_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 하나"), ("f2", "강의 둘"))
    folders = [Folder(id="old", name="이전"), Folder(id="new", name="강의")]
    storage.replace_folders(folders, now=1)
    for file_id in ("f1", "f2"):
        storage.set_file_folders(file_id, ["new"])
    client = FakeClient(folders, {"f1": ["new"], "f2": ["new"]})
    undo_path = tmp_path / "last_classify.json"
    entries = [
        {
            "file_id": file_id,
            "folder_id": "new",
            "folder_name": "강의",
            "title": f"강의 {file_id}",
            "previous_folder_ids": ["old"],
        }
        for file_id in ("f1", "f2")
    ]
    replace_undo_manifest(entries, undo_path)
    real_replace = router_mod.replace_undo_manifest
    hijacked = False

    def replace_then_hijack(moved, path, *, lock_held=False):
        nonlocal hijacked
        result = real_replace(moved, path, lock_held=lock_held)
        if not hijacked and moved and moved[0]["file_id"] == "f2":
            hijacked = True
            document = json.loads(path.read_text(encoding="utf-8"))
            document["moved"][0]["previous_folder_ids"] = []
            path.write_text(json.dumps(document), encoding="utf-8")
        return result

    monkeypatch.setattr(router_mod, "replace_undo_manifest", replace_then_hijack)

    with pytest.raises(UndoJournalError, match="replaced after Cloud restore"):
        undo_saved_manifest(storage, client, undo_path)

    assert client.mutations == [("f1", ["old"])]
    assert client.remote_folders == {"f1": ["old"], "f2": ["new"]}
    assert undo_journal_path(undo_path).exists()


def test_undo_terminal_journal_cleanup_failure_stops_before_next_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 하나"), ("f2", "강의 둘"))
    folders = [Folder(id="old", name="이전"), Folder(id="new", name="강의")]
    storage.replace_folders(folders, now=1)
    for file_id in ("f1", "f2"):
        storage.set_file_folders(file_id, ["new"])
    client = FakeClient(folders, {"f1": ["new"], "f2": ["new"]})
    undo_path = tmp_path / "last_classify.json"
    replace_undo_manifest(
        [
            {
                "file_id": file_id,
                "folder_id": "new",
                "folder_name": "강의",
                "title": f"강의 {file_id}",
                "previous_folder_ids": ["old"],
            }
            for file_id in ("f1", "f2")
        ],
        undo_path,
    )
    real_remove = router_mod._remove_private_file

    def fail_terminal_cleanup(path: Path) -> None:
        if path == undo_journal_path(undo_path):
            raise OSError("simulated terminal WAL unlink failure")
        real_remove(path)

    monkeypatch.setattr(router_mod, "_remove_private_file", fail_terminal_cleanup)

    with pytest.raises(UndoJournalError, match="terminal journal"):
        undo_saved_manifest(storage, client, undo_path)

    assert client.mutations == [("f1", ["old"])]
    assert client.remote_folders == {"f1": ["old"], "f2": ["new"]}
    assert json.loads(undo_journal_path(undo_path).read_text())["phase"] == "committed"
    assert [entry["file_id"] for entry in json.loads(undo_path.read_text())["moved"]] == ["f2"]

    with pytest.raises(UndoJournalError, match="terminal journal"):
        router_mod.reconcile_undo_journal(storage, client, undo_path, resume_cloud=True)

    assert client.mutations == [("f1", ["old"])]


def test_stale_terminal_undo_journal_blocks_a_new_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    folders = [Folder(id="old", name="이전"), Folder(id="new", name="강의")]
    storage.replace_folders(folders, now=1)
    storage.set_file_folders("f1", ["new"])
    client = FakeClient(folders, {"f1": ["new"]})
    undo_path = tmp_path / "last_classify.json"
    replace_undo_manifest(
        [
            {
                "file_id": "f1",
                "folder_id": "new",
                "folder_name": "강의",
                "title": "강의 준비",
                "previous_folder_ids": ["old"],
            }
        ],
        undo_path,
    )
    real_remove = router_mod._remove_private_file

    def fail_terminal_cleanup(path: Path) -> None:
        if path == undo_journal_path(undo_path):
            raise OSError("simulated terminal WAL unlink failure")
        real_remove(path)

    monkeypatch.setattr(router_mod, "_remove_private_file", fail_terminal_cleanup)

    with pytest.raises(UndoJournalError, match="terminal journal"):
        undo_saved_manifest(storage, client, undo_path)

    preview = route_recordings(storage, client, include_filed=True)
    plan_path = tmp_path / "preview.json"
    write_preview_plan(preview, plan_path)

    with pytest.raises(UndoJournalError, match="terminal journal"):
        apply_saved_plan(
            storage,
            client,
            selected_ids=["f1"],
            plan_path=plan_path,
            expected_plan_id=preview.plan_id,
            undo_path=undo_path,
            now=preview.planned_at,
        )

    assert client.mutations == [("f1", ["old"])]
    assert client.remote_folders["f1"] == ["old"]


def test_cleaned_terminal_undo_journal_still_requires_a_separate_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    folders = [Folder(id="old", name="이전"), Folder(id="new", name="강의")]
    storage.replace_folders(folders, now=1)
    storage.set_file_folders("f1", ["new"])
    client = FakeClient(folders, {"f1": ["new"]})
    undo_path = tmp_path / "last_classify.json"
    replace_undo_manifest(
        [
            {
                "file_id": "f1",
                "folder_id": "new",
                "folder_name": "강의",
                "title": "강의 준비",
                "previous_folder_ids": ["old"],
            }
        ],
        undo_path,
    )
    real_remove = router_mod._remove_private_file
    failed = False

    def fail_terminal_cleanup_once(path: Path) -> None:
        nonlocal failed
        if not failed and path == undo_journal_path(undo_path):
            failed = True
            raise OSError("simulated terminal WAL unlink failure")
        real_remove(path)

    monkeypatch.setattr(router_mod, "_remove_private_file", fail_terminal_cleanup_once)

    with pytest.raises(UndoJournalError, match="terminal journal"):
        undo_saved_manifest(storage, client, undo_path)

    preview = route_recordings(storage, client, include_filed=True)
    plan_path = tmp_path / "preview.json"
    write_preview_plan(preview, plan_path)

    with pytest.raises(UndoJournalError, match="separate action"):
        apply_saved_plan(
            storage,
            client,
            selected_ids=["f1"],
            plan_path=plan_path,
            expected_plan_id=preview.plan_id,
            undo_path=undo_path,
            now=preview.planned_at,
        )

    assert not undo_journal_path(undo_path).exists()
    assert client.mutations == [("f1", ["old"])]


def test_undo_recovers_cloud_restore_before_manifest_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    folders = [Folder(id="old", name="이전"), Folder(id="new", name="강의")]
    storage.replace_folders(folders, now=1)
    storage.set_file_folders("f1", ["new"])
    client = FakeClient(folders, {"f1": ["new"]})
    undo_path = tmp_path / "last_classify.json"
    replace_undo_manifest(
        [
            {
                "file_id": "f1",
                "folder_id": "new",
                "folder_name": "강의",
                "title": "강의 준비",
                "previous_folder_ids": ["old"],
            }
        ],
        undo_path,
    )
    real_replace = router_mod.replace_undo_manifest
    failed = False

    def fail_once(moved, path, *, lock_held=False):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("crash before manifest rewrite")
        return real_replace(moved, path, lock_held=lock_held)

    monkeypatch.setattr(router_mod, "replace_undo_manifest", fail_once)

    with pytest.raises(UndoJournalError, match="manifest needs recovery"):
        undo_saved_manifest(storage, client, undo_path)

    assert client.remote_folders["f1"] == ["old"]
    assert undo_path.exists()
    assert undo_journal_path(undo_path).exists()
    recovered = undo_saved_manifest(storage, client, undo_path)
    assert recovered.reverted_count == 1
    assert client.mutations == [("f1", ["old"])]
    assert not undo_path.exists()
    assert not undo_journal_path(undo_path).exists()


def test_undo_recovers_process_crash_after_intent_before_patch(
    tmp_path: Path,
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    folders = [Folder(id="old", name="이전"), Folder(id="new", name="강의")]
    storage.replace_folders(folders, now=1)
    storage.set_file_folders("f1", ["new"])

    class CrashThenSuccess(FakeClient):
        crash_once = True

        def set_file_folders_once(self, file_id: str, folder_ids: list[str]) -> None:
            if self.crash_once:
                self.crash_once = False
                raise KeyboardInterrupt
            super().set_file_folders_once(file_id, folder_ids)

    client = CrashThenSuccess(folders, {"f1": ["new"]})
    undo_path = tmp_path / "last_classify.json"
    replace_undo_manifest(
        [
            {
                "file_id": "f1",
                "folder_id": "new",
                "folder_name": "강의",
                "title": "강의 준비",
                "previous_folder_ids": ["old"],
            }
        ],
        undo_path,
    )

    with pytest.raises(KeyboardInterrupt):
        undo_saved_manifest(storage, client, undo_path)

    assert undo_path.exists()
    assert undo_journal_path(undo_path).exists()
    recovered = undo_saved_manifest(storage, client, undo_path)
    assert recovered.reverted_count == 1
    assert client.mutations == [("f1", ["old"])]
    assert not undo_path.exists()


def test_undo_recovers_manifest_rewrite_before_journal_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    folders = [Folder(id="old", name="이전"), Folder(id="new", name="강의")]
    storage.replace_folders(folders, now=1)
    storage.set_file_folders("f1", ["new"])
    client = FakeClient(folders, {"f1": ["new"]})
    undo_path = tmp_path / "last_classify.json"
    replace_undo_manifest(
        [
            {
                "file_id": "f1",
                "folder_id": "new",
                "folder_name": "강의",
                "title": "강의 준비",
                "previous_folder_ids": ["old"],
            }
        ],
        undo_path,
    )
    real_write = router_mod._write_undo_journal
    failed = False

    def fail_committed_once(path, journal):
        nonlocal failed
        if not failed and journal["phase"] == "committed":
            failed = True
            raise OSError("crash after manifest rewrite")
        real_write(path, journal)

    monkeypatch.setattr(router_mod, "_write_undo_journal", fail_committed_once)

    with pytest.raises(UndoJournalError, match="needs cleanup"):
        undo_saved_manifest(storage, client, undo_path)

    assert not undo_path.exists()
    assert undo_journal_path(undo_path).exists()
    recovered = undo_saved_manifest(storage, client, undo_path)
    assert recovered.reverted_count == 1
    assert client.mutations == [("f1", ["old"])]
    assert not undo_journal_path(undo_path).exists()


def test_undo_ambiguous_timeout_without_commit_keeps_journal_and_resumes_exact_restore(
    tmp_path: Path,
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    folders = [Folder(id="old", name="이전"), Folder(id="new", name="강의")]
    storage.replace_folders(folders, now=1)
    storage.set_file_folders("f1", ["new"])

    class TimeoutThenSuccess(FakeClient):
        timeout_once = True

        def set_file_folders_once(self, file_id: str, folder_ids: list[str]) -> None:
            if self.timeout_once:
                self.timeout_once = False
                raise TimeoutError("ambiguous transport timeout")
            super().set_file_folders_once(file_id, folder_ids)

    client = TimeoutThenSuccess(folders, {"f1": ["new"]})
    undo_path = tmp_path / "last_classify.json"
    replace_undo_manifest(
        [
            {
                "file_id": "f1",
                "folder_id": "new",
                "folder_name": "강의",
                "title": "강의 준비",
                "previous_folder_ids": ["old"],
            }
        ],
        undo_path,
    )

    with pytest.raises(UndoJournalError, match="uncertain"):
        undo_saved_manifest(storage, client, undo_path)

    assert undo_path.exists()
    assert undo_journal_path(undo_path).exists()
    recovered = undo_saved_manifest(storage, client, undo_path)
    assert recovered.reverted_count == 1
    assert client.mutations == [("f1", ["old"])]
    assert not undo_path.exists()
    assert not undo_journal_path(undo_path).exists()


@pytest.mark.parametrize("status_code", [None, 504])
def test_undo_wrapped_plaud_ambiguous_error_keeps_attempting_wal(
    tmp_path: Path,
    status_code: int | None,
) -> None:
    storage = _storage(tmp_path, ("f1", "강의 준비"))
    folders = [Folder(id="old", name="이전"), Folder(id="new", name="강의")]
    storage.replace_folders(folders, now=1)
    storage.set_file_folders("f1", ["new"])

    class WrappedNetworkFailure(FakeClient):
        def set_file_folders_once(self, file_id: str, folder_ids: list[str]) -> None:
            raise PlaudAPIError("wrapped network failure", status_code=status_code)

    client = WrappedNetworkFailure(folders, {"f1": ["new"]})
    undo_path = tmp_path / "last_classify.json"
    replace_undo_manifest(
        [
            {
                "file_id": "f1",
                "folder_id": "new",
                "folder_name": "강의",
                "title": "강의 준비",
                "previous_folder_ids": ["old"],
            }
        ],
        undo_path,
    )

    with pytest.raises(UndoJournalError, match="uncertain"):
        undo_saved_manifest(storage, client, undo_path)

    assert undo_journal_path(undo_path).exists()
    assert undo_path.exists()


def test_deterministic_route_has_no_private_static_taxonomy() -> None:
    catalog = FolderCatalog([Folder(id="custom-123", name="나만의 고객")])
    snapshot = RecordingSnapshot(file_id="f", title="나만의 고객 미팅")
    from core.community_router import deterministic_route

    decision = deterministic_route(snapshot, catalog)
    assert decision.folder_id == "custom-123"
    assert decision.folder_name == "나만의 고객"
