from __future__ import annotations

import inspect
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from windows_app.service import CommunityService, ServiceError


@dataclass
class AppPaths:
    data_dir: Path
    export_dir: Path
    env_file: Path


class ReadStorage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def counts(self):
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM files WHERE is_trash = 0").fetchone()[0]
            cached = conn.execute("SELECT COUNT(*) FROM file_content").fetchone()[0]
        return {"total": total, "trash": 0, "folders": 1, "cached": cached}

    def get_file_row(self, file_id):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()

    def get_content_row(self, file_id):
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM file_content WHERE file_id = ?", (file_id,)
            ).fetchone()

    def search_recordings(self, query, *, limit):
        if query == "회의":
            return [{"file_id": "rec-1", "snippet": "회의에서 결정한 내용"}]
        return []


def _read_storage(tmp_path):
    db = tmp_path / "plaud.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE files (
            id TEXT PRIMARY KEY, filename TEXT, duration REAL, edit_time INTEGER,
            start_time INTEGER, is_trash INTEGER, starred INTEGER
        );
        CREATE TABLE file_content (
            file_id TEXT PRIMARY KEY, title TEXT, transcript TEXT, outline TEXT,
            summary_md TEXT, summary_extra TEXT, keywords TEXT, fetched_at INTEGER
        );
        CREATE TABLE file_folders (file_id TEXT, folder_id TEXT);
        CREATE TABLE folders (id TEXT PRIMARY KEY, name TEXT);
        INSERT INTO files VALUES ('rec-1', '참가자 회의', 125, 1700000000, 1699999000, 0, 0);
        INSERT INTO folders VALUES ('folder-1', '업무');
        INSERT INTO file_folders VALUES ('rec-1', 'folder-1');
        """
    )
    conn.execute(
        "INSERT INTO file_content VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "rec-1",
            "참가자 회의",
            json.dumps(
                [{"start_time": 0, "end_time": 1000, "speaker": "A", "content": "안녕하세요"}],
                ensure_ascii=False,
            ),
            json.dumps([{"start_time": 0, "end_time": 1000, "topic": "시작"}]),
            "결정 사항",
            json.dumps(["추가 노트"], ensure_ascii=False),
            json.dumps(["회의"], ensure_ascii=False),
            1700000100,
        ),
    )
    conn.commit()
    conn.close()
    return ReadStorage(db)


def _paths(tmp_path):
    export_dir = tmp_path / "exports"
    export_dir.mkdir(exist_ok=True)
    return AppPaths(tmp_path, export_dir, tmp_path / "settings.env")


def _auth():
    return SimpleNamespace(configured=True, state="valid", remaining_human="2h")


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
    assert service.search("회의")["items"][0]["id"] == "rec-1"

    exported = service.export("rec-1", "transcript")
    target = _paths(tmp_path).export_dir / exported["file"]
    assert target.parent == (tmp_path / "exports")
    assert "안녕하세요" in target.read_text(encoding="utf-8")


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


def test_curl_live_rejection_and_unreachable_are_distinct(tmp_path):
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
    result = unreachable.import_curl("curl while network is unavailable")
    assert result["status"] == "connected_unverified"
    assert result["verification"] == "unreachable"
    assert "curl while" not in json.dumps(result)
