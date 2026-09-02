"""Full-content recording search (FTS5 trigram)."""

from pathlib import Path

from core.models import FileContent, SummaryBlock, TranscriptSegment
from core.storage import Storage


def _seed(tmp_path: Path) -> Storage:
    storage = Storage(db_path=tmp_path / "t.db")
    storage.save_content(
        FileContent(
            file_id="a",
            title="AI 에이전트 구축 회의",
            transcript=[
                TranscriptSegment(start_time=0, end_time=1000, content="메타데이터 검색 논의"),
                TranscriptSegment(start_time=1000, end_time=2000, content="vault linking plan"),
            ],
            outline=[],
            summaries=[SummaryBlock(kind="auto_sum_note", body_md="요약: 지식 관리 시스템")],
            keywords=[],
            folder_ids=[],
        ),
        now=1,
    )
    storage.save_content(
        FileContent(
            file_id="b",
            title="Jazz session",
            transcript=[TranscriptSegment(start_time=0, end_time=1, content="improvisation")],
            outline=[],
            summaries=[],
            keywords=[],
            folder_ids=[],
        ),
        now=1,
    )
    return storage


def test_search_korean_substring_in_transcript(tmp_path: Path) -> None:
    storage = _seed(tmp_path)
    hits = {h["file_id"] for h in storage.search_recordings("데이터")}  # substring of 메타데이터
    assert hits == {"a"}


def test_search_matches_title_and_summary(tmp_path: Path) -> None:
    storage = _seed(tmp_path)
    assert {h["file_id"] for h in storage.search_recordings("에이전트")} == {"a"}
    assert {h["file_id"] for h in storage.search_recordings("지식 관리")} == {"a"}
    assert {h["file_id"] for h in storage.search_recordings("improvisation")} == {"b"}


def test_search_multi_term_is_and(tmp_path: Path) -> None:
    storage = _seed(tmp_path)
    # both terms in 'a', not in 'b'
    assert {h["file_id"] for h in storage.search_recordings("에이전트 메타데이터")} == {"a"}
    # one term missing → no match
    assert storage.search_recordings("에이전트 improvisation") == []


def test_search_empty_query_returns_nothing(tmp_path: Path) -> None:
    storage = _seed(tmp_path)
    assert storage.search_recordings("   ") == []


def test_reindex_counts_rows(tmp_path: Path) -> None:
    storage = _seed(tmp_path)
    assert storage.rebuild_search_index() == 2


def test_save_content_keeps_index_in_sync(tmp_path: Path) -> None:
    storage = _seed(tmp_path)
    # Re-save 'a' with new content; the old term should disappear.
    storage.save_content(
        FileContent(
            file_id="a",
            title="완전히 다른 제목",
            transcript=[TranscriptSegment(start_time=0, end_time=1, content="새로운 내용")],
            outline=[],
            summaries=[],
            keywords=[],
            folder_ids=[],
        ),
        now=2,
    )
    assert storage.search_recordings("메타데이터") == []
    assert {h["file_id"] for h in storage.search_recordings("새로운")} == {"a"}
