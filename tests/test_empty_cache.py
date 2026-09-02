"""Empty Plaud results must not be cached as final (the 'no content' bug)."""

from pathlib import Path

from core.models import FileContent, SummaryBlock, TranscriptSegment
from core.storage import Storage


def _content(file_id: str, *, transcript=False, summary=False, folders=None) -> FileContent:
    return FileContent(
        file_id=file_id,
        title="t",
        transcript=(
            [TranscriptSegment(start_time=0, end_time=1, content="hi")] if transcript else []
        ),
        outline=[],
        summaries=([SummaryBlock(kind="auto_sum_note", body_md="s")] if summary else []),
        keywords=[],
        folder_ids=folders or [],
    )


def test_is_empty_property() -> None:
    assert _content("a").is_empty is True
    assert _content("a", transcript=True).is_empty is False
    assert _content("a", summary=True).is_empty is False


def test_save_content_skips_empty_but_keeps_folders(tmp_path: Path) -> None:
    storage = Storage(db_path=tmp_path / "t.db")
    storage.save_content(_content("a", folders=["f1"]), now=1)
    # No content row was written (empty result not cached)…
    assert storage.get_content_row("a") is None
    assert "a" not in storage.cached_file_ids()
    # …but the folder assignment still synced.
    with storage._connect() as conn:
        folders = [
            r[0] for r in conn.execute("SELECT folder_id FROM file_folders WHERE file_id='a'")
        ]
    assert folders == ["f1"]


def test_save_content_persists_nonempty(tmp_path: Path) -> None:
    storage = Storage(db_path=tmp_path / "t.db")
    storage.save_content(_content("a", transcript=True), now=1)
    assert storage.get_content_row("a") is not None
    assert "a" in storage.cached_file_ids()


def test_delete_empty_content_clears_stale_rows(tmp_path: Path) -> None:
    storage = Storage(db_path=tmp_path / "t.db")
    # Force an empty row in the way the old buggy backfill did (direct insert).
    with storage._connect() as conn:
        conn.execute(
            "INSERT INTO file_content (file_id, title, transcript, outline, summary_md, "
            "summary_extra, keywords, fetched_at) VALUES ('stale','t','[]','[]',NULL,'[]','[]',1)"
        )
        conn.execute(
            "INSERT INTO file_content (file_id, title, transcript, outline, summary_md, "
            "summary_extra, keywords, fetched_at) VALUES ('good','t','[{\"x\":1}]','[]','sum','[]','[]',1)"
        )
    cleared = storage.delete_empty_content()
    assert cleared == ["stale"]
    assert storage.get_content_row("stale") is None
    assert storage.get_content_row("good") is not None
