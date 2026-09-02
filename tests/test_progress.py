"""Derived progress stages (core/progress.py)."""

from pathlib import Path

from core.progress import STAGES, derive_progress, integrated_file_ids
from core.storage import Storage


def _seed(tmp_path: Path) -> Storage:
    storage = Storage(db_path=tmp_path / "test.db")
    with storage._connect() as conn:
        conn.executemany(
            "INSERT INTO files (id, filename, is_trash, synced_at, updated_at)"
            " VALUES (?, ?, ?, 1, 1)",
            [
                ("f-new", "new file", 0),
                ("f-cached", "cached file", 0),
                ("f-transcribed", "transcribed file", 0),
                ("f-integrated", "integrated file", 0),
                ("f-trash", "trashed file", 1),
            ],
        )
        conn.executemany(
            "INSERT INTO file_content (file_id, fetched_at) VALUES (?, 1)",
            [("f-cached",), ("f-transcribed",), ("f-integrated",), ("f-trash",)],
        )
        conn.executemany(
            "INSERT INTO cmds_transcripts (file_id, model, fetched_at) VALUES (?, 'scribe', 1)",
            [("f-transcribed",), ("f-integrated",)],
        )
    return storage


def _integrated_root(tmp_path: Path) -> Path:
    root = tmp_path / "integrated"
    (root / "f-integrated").mkdir(parents=True)
    (root / "f-integrated" / "claude__default.md").write_text("x", encoding="utf-8")
    (root / "f-empty-dir").mkdir()  # dir without .md must NOT count
    return root


def test_derive_progress_assigns_highest_reached_stage(tmp_path: Path) -> None:
    storage = _seed(tmp_path)
    prog = derive_progress(storage, integrated_root=_integrated_root(tmp_path))

    assert prog.stages == {
        "f-new": "new",
        "f-cached": "cached",
        "f-transcribed": "transcribed",
        "f-integrated": "integrated",
    }
    assert prog.counts == {"new": 1, "cached": 1, "transcribed": 1, "integrated": 1}
    assert "f-trash" not in prog.stages  # trash excluded entirely


def test_integrated_file_ids_requires_md_inside_dir(tmp_path: Path) -> None:
    root = _integrated_root(tmp_path)
    assert integrated_file_ids(root) == {"f-integrated"}
    assert integrated_file_ids(tmp_path / "missing") == set()


def test_counts_cover_every_stage_key(tmp_path: Path) -> None:
    storage = Storage(db_path=tmp_path / "empty.db")
    prog = derive_progress(storage, integrated_root=tmp_path / "none")
    assert set(prog.counts) == set(STAGES)
    assert sum(prog.counts.values()) == 0
