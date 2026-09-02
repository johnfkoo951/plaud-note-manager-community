import json
import time

from core.models import FileContent, Folder, PlaudFile, SummaryBlock, TranscriptSegment
from core.storage import Storage
from core.tags import normalize_tag, normalize_tags


def test_storage_saves_content_and_folder_links(tmp_path) -> None:
    storage = Storage(tmp_path / "plaud.db")
    now = int(time.time())

    storage.replace_folders(
        [Folder(id="folder-1", name="Meetings", color="#4c8eff")],
        now=now,
    )
    storage.upsert_file(
        PlaudFile(id="file-1", filename="recording.m4a", duration=120_000),
        now=now,
    )
    storage.save_content(
        FileContent(
            file_id="file-1",
            title="Recording",
            transcript=[
                TranscriptSegment(
                    start_time=0,
                    end_time=1000,
                    speaker="speaker_0",
                    content="hello",
                )
            ],
            summaries=[SummaryBlock(kind="auto_sum_note", body_md="summary")],
            keywords=["test"],
            folder_ids=["folder-1"],
        ),
        now=now,
    )

    row = storage.get_content_row("file-1")

    assert row is not None
    assert row["title"] == "Recording"
    assert json.loads(row["keywords"]) == ["test"]
    with storage._connect() as conn:
        folder_rows = list(conn.execute("SELECT * FROM file_folders"))
    assert [(r["file_id"], r["folder_id"]) for r in folder_rows] == [("file-1", "folder-1")]


def test_note_metadata_and_tags_are_keyed_by_file_id(tmp_path) -> None:
    storage = Storage(tmp_path / "plaud.db")
    now = int(time.time())

    storage.upsert_note_metadata(
        file_id="file-1",
        title="Original Title",
        description="Stable metadata for a Plaud note.",
        note_type="meeting",
        status="inProgress",
        metadata={"stable_id": "file-1", "title": "Original Title"},
        now=now,
    )
    added = storage.add_note_tags(
        "file-1",
        ["#Meeting Minutes", "AI 요약", "AI 요약"],
        source="manual",
        now=now,
    )
    storage.upsert_note_reference(
        file_id="file-1",
        path=tmp_path / "note.md",
        kind="meeting-note",
        title="Original Title",
        now=now,
    )

    assert added == ["Meeting-Minutes", "AI-요약"]
    row = storage.get_note_metadata("file-1")
    tags = storage.list_note_tags("file-1")
    refs = storage.list_note_references("file-1")

    assert row is not None
    assert row["file_id"] == "file-1"
    assert row["note_type"] == "meeting"
    assert [t["tag"] for t in tags] == ["AI-요약", "Meeting-Minutes"]
    assert refs[0]["kind"] == "meeting-note"


def test_normalize_tag_removes_hashes_and_spaces() -> None:
    assert normalize_tag("#옵시디언 회의록") == "옵시디언-회의록"
    assert normalize_tag("  AI / PKM  ") == "AI-PKM"
    assert normalize_tags(["AI, 회의록"]) == ["AI", "회의록"]
