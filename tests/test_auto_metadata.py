from __future__ import annotations

import time

from core import auto_metadata
from core.auto_metadata import run_auto_metadata
from core.models import FileContent, PlaudFile, SummaryBlock, TranscriptSegment
from core.storage import Storage


def _seed(tmp_path, count: int = 3) -> Storage:
    storage = Storage(tmp_path / "plaud.db")
    now = int(time.time())
    for i in range(count):
        fid = f"f{i}"
        storage.upsert_file(PlaudFile(id=fid, filename=f"{fid}.m4a", duration=1000), now=now)
        storage.save_content(
            FileContent(
                file_id=fid,
                title=f"Rec {i}",
                transcript=[
                    TranscriptSegment(
                        start_time=0, end_time=10, speaker="speaker_0", content=f"content {i}"
                    )
                ],
                summaries=[SummaryBlock(kind="auto_sum_note", body_md="s")],
                keywords=[],
                folder_ids=[],
            ),
            now=now,
        )
    return storage


def test_batch_without_consent_aborts_before_touching_a_provider(tmp_path, monkeypatch) -> None:
    storage = _seed(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        auto_metadata, "run_auto_metadata", run_auto_metadata
    )  # keep the real orchestration

    report = run_auto_metadata(storage, provider="openai", backend="api")

    assert report.aborted
    assert "confirmation" in report.aborted
    assert report.generated == []
    assert calls == []


def test_dry_run_reports_candidates_without_consent_or_calls(tmp_path) -> None:
    storage = _seed(tmp_path)
    report = run_auto_metadata(storage, provider="openai", backend="api", dry_run=True)
    assert sorted(report.generated) == ["f0", "f1", "f2"]


def test_unchanged_source_is_skipped_on_the_second_run(tmp_path, monkeypatch) -> None:
    storage = _seed(tmp_path, count=1)
    import core.community_metadata as gen
    import core.community_models as models

    monkeypatch.setattr(models, "model_available", lambda *a, **k: True)
    monkeypatch.setattr(
        gen, "generate_note_metadata", lambda *a, **k: {"title": "T", "file_id": "f0"}
    )

    first = run_auto_metadata(
        storage, provider="openai", backend="api", confirmed_external=True
    )
    assert first.generated == ["f0"]

    second = run_auto_metadata(
        storage, provider="openai", backend="api", confirmed_external=True
    )
    assert second.generated == []
    assert second.unchanged == ["f0"]


def test_circuit_breaker_stops_the_batch_and_reports_untried_files(tmp_path, monkeypatch) -> None:
    storage = _seed(tmp_path, count=5)
    import core.community_metadata as gen
    import core.community_models as models

    monkeypatch.setattr(models, "model_available", lambda *a, **k: True)

    def always_fails(*a, **k):
        raise RuntimeError("401 invalid api key")

    monkeypatch.setattr(gen, "generate_note_metadata", always_fails)
    # Stop as soon as two calls in a row have failed.
    monkeypatch.setattr(auto_metadata, "should_abort_batch", lambda c, g, a: c >= 2)

    report = run_auto_metadata(
        storage, provider="openai", backend="api", confirmed_external=True
    )

    assert len(report.failed) == 2, "should stop instead of burning every file"
    assert report.aborted and "consecutive failures" in report.aborted
    assert report.remaining >= 3


def test_stop_rule_tolerates_isolated_failures_and_stops_on_three_in_a_row() -> None:
    assert auto_metadata.should_abort_batch(0, 5, 5) is False
    assert auto_metadata.should_abort_batch(1, 4, 5) is False
    assert auto_metadata.should_abort_batch(2, 3, 5) is False
    assert auto_metadata.should_abort_batch(3, 2, 5) is True
    assert auto_metadata.should_abort_batch(9, 0, 9) is True
