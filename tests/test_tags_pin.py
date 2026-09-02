"""Tag counts, pinned-tag config, and classify-undo manifest behavior."""

from pathlib import Path

import pytest

from core import app_config
from core.models import PlaudFile
from core.storage import Storage


def _seed_tags(tmp_path: Path) -> Storage:
    storage = Storage(db_path=tmp_path / "t.db")
    storage.upsert_file(PlaudFile(id="a", filename="A"), now=1)
    storage.upsert_file(PlaudFile(id="b", filename="B"), now=1)
    storage.upsert_file(PlaudFile(id="t", filename="trashed"), now=1, is_trash=1)
    storage.add_note_tags("a", ["meeting", "lg"], source="manual", now=1)
    storage.add_note_tags("b", ["meeting"], source="manual", now=1)
    storage.add_note_tags("t", ["meeting"], source="manual", now=1)  # trashed
    return storage


def test_tag_counts_excludes_trash_and_sorts_by_frequency(tmp_path: Path) -> None:
    storage = _seed_tags(tmp_path)
    counts = dict(storage.tag_counts())
    assert counts["meeting"] == 2  # 'a' + 'b', not the trashed 't'
    assert counts["lg"] == 1
    # busiest first
    assert storage.tag_counts()[0][0] == "meeting"


def test_file_ids_with_tag(tmp_path: Path) -> None:
    storage = _seed_tags(tmp_path)
    assert storage.file_ids_with_tag("lg") == {"a"}
    assert storage.file_ids_with_tag("meeting") == {"a", "b", "t"}


def test_pinned_tags_roundtrip_and_toggle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "CONFIG_FILE", tmp_path / "config.json")
    assert app_config.pinned_tags() == []
    assert app_config.toggle_pinned_tag("lg") is True
    assert app_config.pinned_tags() == ["lg"]
    assert app_config.toggle_pinned_tag("lg") is False
    assert app_config.pinned_tags() == []
    app_config.set_pinned_tags(["a", "a", "b"])  # de-dupes, preserves order
    assert app_config.pinned_tags() == ["a", "b"]
