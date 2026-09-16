from __future__ import annotations

import time

import pytest

from core import community_metadata, community_models
from core.community_metadata import MetadataGenerationError, generate_note_metadata
from core.models import FileContent, PlaudFile, SummaryBlock, TranscriptSegment
from core.storage import Storage


def _seed(tmp_path, *, transcript: str = "we agreed to ship on friday") -> Storage:
    storage = Storage(tmp_path / "plaud.db")
    now = int(time.time())
    storage.upsert_file(PlaudFile(id="f1", filename="rec.m4a", duration=1000), now=now)
    storage.save_content(
        FileContent(
            file_id="f1",
            title="Recording",
            transcript=[
                TranscriptSegment(start_time=0, end_time=10, speaker="speaker_0", content=transcript)
            ],
            summaries=[SummaryBlock(kind="auto_sum_note", body_md="a summary")],
            keywords=[],
            folder_ids=[],
        ),
        now=now,
    )
    return storage


# ----- reply parsing ----------------------------------------------------


def test_extract_json_accepts_fenced_and_padded_replies() -> None:
    want = {"title": "T"}
    assert community_metadata._extract_json('{"title": "T"}') == want
    assert community_metadata._extract_json('```json\n{"title": "T"}\n```') == want
    assert community_metadata._extract_json('Sure!\n{"title": "T"}\nHope that helps.') == want


def test_extract_json_rejects_a_reply_with_no_object() -> None:
    with pytest.raises(MetadataGenerationError):
        community_metadata._extract_json("I cannot help with that.")


# ----- field cleaning ---------------------------------------------------


def test_clean_keeps_only_owned_fields_and_validates_note_type() -> None:
    cleaned = community_metadata._clean(
        {
            "title": "Weekly sync",
            "description": "Team agreed on the release date.",
            "note_type": "MEETING",
            "tags": ["Release", "planning"],
            # fields this edition must never accept from a provider
            "vault_dest": "/Users/someone/vault",
            "cmds": "30. Projects",
            "category": "personal",
        }
    )
    assert cleaned["title"] == "Weekly sync"
    assert cleaned["note_type"] == "meeting"
    assert cleaned["tags"]
    assert "vault_dest" not in cleaned
    assert "cmds" not in cleaned
    assert "category" not in cleaned


def test_clean_drops_an_unknown_note_type() -> None:
    assert "note_type" not in community_metadata._clean({"note_type": "podcast"})


# ----- consent ----------------------------------------------------------


def test_generation_without_consent_never_calls_the_provider(tmp_path, monkeypatch) -> None:
    storage = _seed(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        community_models, "_run_api", lambda *a, **k: calls.append("api") or "{}"
    )
    monkeypatch.setattr(community_models, "_run_cli", lambda *a, **k: calls.append("cli") or "{}")

    with pytest.raises(community_models.ExternalConsentRequired):
        generate_note_metadata(storage, "f1", provider="openai", backend="api")

    assert calls == []


def test_generation_with_consent_returns_merged_local_metadata(tmp_path, monkeypatch) -> None:
    storage = _seed(tmp_path)
    monkeypatch.setattr(
        community_models,
        "run_model",
        lambda *a, **k: '{"title":"Ship review","description":"Agreed on friday.",'
        '"note_type":"meeting","tags":["release"]}',
    )
    monkeypatch.setattr(community_metadata, "community_models", community_models)

    merged = generate_note_metadata(
        storage, "f1", provider="openai", backend="api", confirmed_external=True
    )

    assert merged["title"] == "Ship review"
    assert merged["note_type"] == "meeting"
    assert merged["file_id"] == "f1"
    assert merged["usage_status"] == "metadata-ready"
    assert "vault_dest" not in merged


def test_generation_rejects_a_file_with_no_cached_source(tmp_path) -> None:
    storage = Storage(tmp_path / "plaud.db")
    storage.upsert_file(PlaudFile(id="empty", filename="x.m4a", duration=1), now=int(time.time()))
    with pytest.raises(MetadataGenerationError):
        generate_note_metadata(
            storage, "empty", provider="openai", backend="api", confirmed_external=True
        )
