"""Local-only seen/starred flags and the classify-model config."""

from pathlib import Path

import pytest

from core import app_config
from core.models import PlaudFile
from core.storage import Storage


def test_mark_seen_only_sets_once(tmp_path: Path) -> None:
    storage = Storage(db_path=tmp_path / "t.db")
    storage.upsert_file(PlaudFile(id="f1", filename="a"), now=1)
    storage.mark_seen("f1", now=100)
    storage.mark_seen("f1", now=200)  # must not move the original timestamp
    row = storage.get_file_row("f1")
    assert row["seen_at"] == 100


def test_starred_and_seen_survive_cloud_sync_upsert(tmp_path: Path) -> None:
    storage = Storage(db_path=tmp_path / "t.db")
    storage.upsert_file(PlaudFile(id="f1", filename="a"), now=1)
    storage.mark_seen("f1", now=50)
    storage.set_starred("f1", True)

    # A later sync upserts the same file — local-only flags must survive.
    storage.upsert_file(PlaudFile(id="f1", filename="renamed"), now=99)
    row = storage.get_file_row("f1")
    assert row["filename"] == "renamed"
    assert row["seen_at"] == 50
    assert row["starred"] == 1

    storage.set_starred("f1", False)
    assert storage.get_file_row("f1")["starred"] == 0


def test_migration_backfills_existing_library_as_seen(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    # Minimal v1-era files table: no seen_at / starred columns.
    conn.execute(
        """CREATE TABLE files (
            id TEXT PRIMARY KEY, filename TEXT, filesize INTEGER, duration REAL,
            edit_time INTEGER, start_time INTEGER,
            is_trash INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'new', local_path TEXT,
            synced_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO files (id, filename, synced_at, updated_at) VALUES ('old', 'x', 1, 1)"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    storage = Storage(db_path=db)  # runs the v2 migration
    old_row = storage.get_file_row("old")
    assert old_row["seen_at"] is not None  # pre-existing library counts as seen
    assert old_row["starred"] == 0

    storage.upsert_file(PlaudFile(id="new", filename="fresh"), now=2)
    assert storage.get_file_row("new")["seen_at"] is None  # new arrivals = unread


def test_classify_model_config_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "CONFIG_FILE", tmp_path / "config.json")
    assert app_config.classify_model() == "claude"  # default
    app_config.set_classify_model("grok")
    assert app_config.classify_model() == "grok"
    # Other keys keep their defaults after the partial write.
    assert app_config.backend_for("claude") == "cli"
