import time

import pytest

from core import disclosure
from core.models import FileContent, Folder, PlaudFile, SummaryBlock, TranscriptSegment
from core.storage import Storage


def _seed(tmp_path) -> Storage:
    """Build a Storage-only DB with two files, content, a folder, and a tag."""
    storage = Storage(tmp_path / "plaud.db")
    now = int(time.time())

    storage.replace_folders(
        [Folder(id="folder-1", name="Meetings", color="#4c8eff")],
        now=now,
    )
    storage.upsert_file(
        PlaudFile(
            id="file-1",
            filename="weekly-sync.m4a",
            duration=323_000,
            start_time=1777520000000,
            edit_time=1777520,
        ),
        now=now,
    )
    storage.upsert_file(
        PlaudFile(id="file-2", filename="standup.m4a", duration=60_000),
        now=now,
    )
    storage.save_content(
        FileContent(
            file_id="file-1",
            title="Weekly Sync",
            transcript=[
                TranscriptSegment(
                    start_time=0,
                    end_time=1000,
                    speaker="Me",
                    content="hello team",
                )
            ],
            outline=[],
            summaries=[SummaryBlock(kind="auto_sum_note", body_md="discussed Q2 plans")],
            keywords=["obsidian"],
            folder_ids=["folder-1"],
        ),
        now=now,
    )
    storage.add_note_tags("file-1", ["AI 요약"], source="manual", now=now)
    return storage


@pytest.fixture
def db(tmp_path, monkeypatch):
    storage = _seed(tmp_path)
    monkeypatch.setattr(disclosure, "DEFAULT_DB", storage._db_path)
    return storage


# ----- _truncate (pure unit) --------------------------------------------


def test_truncate_short_text_unchanged() -> None:
    assert disclosure._truncate("hello") == "hello"


def test_truncate_none_and_empty_return_none() -> None:
    assert disclosure._truncate(None) is None
    assert disclosure._truncate("") is None


def test_truncate_strips_whitespace() -> None:
    assert disclosure._truncate("  padded  ") == "padded"


def test_truncate_long_text_appends_ellipsis() -> None:
    text = "x" * 50
    out = disclosure._truncate(text, chars=10)
    assert out == "x" * 10 + "…"
    assert out.endswith("…")
    assert len(out) == 11


def test_truncate_exact_length_no_ellipsis() -> None:
    text = "y" * 10
    out = disclosure._truncate(text, chars=10)
    assert out == text
    assert "…" not in out


# ----- peek (L0) ---------------------------------------------------------


def test_peek_returns_expected_fields(db) -> None:
    p = disclosure.peek("file-1")

    assert isinstance(p, disclosure.L0Peek)
    assert p.file_id == "file-1"
    assert p.filename == "weekly-sync.m4a"
    assert p.duration_ms == 323_000
    assert p.folders == ["Meetings"]
    assert p.is_trash is False
    assert p.has_content_cache is True
    assert p.has_cmds_transcript is False
    assert p.integrated_count == 0


def test_peek_unknown_returns_none(db) -> None:
    assert disclosure.peek("does-not-exist") is None


def test_peek_file_without_content(db) -> None:
    p = disclosure.peek("file-2")

    assert p is not None
    assert p.filename == "standup.m4a"
    assert p.has_content_cache is False
    assert p.folders == []


# ----- layer supersets ---------------------------------------------------


def test_brief_is_superset_of_peek(db) -> None:
    p = disclosure.peek("file-1")
    b = disclosure.brief("file-1")

    assert isinstance(b, disclosure.L1Brief)
    # every L0 field is present and equal on the L1 record
    for fld in vars(p):
        assert getattr(b, fld) == getattr(p, fld)
    # L1-specific fields
    assert b.title == "Weekly Sync"
    assert b.keywords == ["obsidian"]
    assert b.tags == ["AI-요약"]
    # speakers come from the cmds_transcripts table (not seeded here)
    assert b.speakers == []


def test_outline_is_superset_of_brief(db) -> None:
    b = disclosure.brief("file-1")
    o = disclosure.outline("file-1")

    assert isinstance(o, disclosure.L2Outline)
    for fld in vars(b):
        assert getattr(o, fld) == getattr(b, fld)
    assert o.plaud_summary_preview == "discussed Q2 plans"


def test_deep_is_superset_of_outline(db) -> None:
    o = disclosure.outline("file-1")
    d = disclosure.deep("file-1")

    assert isinstance(d, disclosure.L3Deep)
    for fld in vars(o):
        assert getattr(d, fld) == getattr(o, fld)
    assert d.plaud_summary == "discussed Q2 plans"
    assert d.vault_link_details == []


def test_layers_none_for_unknown(db) -> None:
    assert disclosure.brief("nope") is None
    assert disclosure.outline("nope") is None
    assert disclosure.deep("nope") is None


# ----- search: Storage-only filters (folder / tag) -----------------------


def test_search_by_folder(db) -> None:
    results = disclosure.search(folder="Meetings")

    assert [b.file_id for b in results] == ["file-1"]
    assert all(isinstance(b, disclosure.L1Brief) for b in results)


def test_search_by_folder_no_match(db) -> None:
    assert disclosure.search(folder="Nonexistent") == []


def test_search_by_tag(db) -> None:
    results = disclosure.search(tag="AI-요약")

    assert [b.file_id for b in results] == ["file-1"]


def test_search_by_tag_no_match(db) -> None:
    assert disclosure.search(tag="Missing") == []


# ----- search: POST-FIX guard on absent vault_index tables ---------------


def test_search_keyword_returns_empty_without_vault_tables(db) -> None:
    # file_keywords / keywords are created by vault_index, not Storage.
    # The guarded OperationalError path must return [] rather than raise.
    assert disclosure.search(keyword="anything") == []


def test_search_vault_note_title_returns_empty_without_vault_tables(db) -> None:
    assert disclosure.search(vault_note_title="Some Note") == []
