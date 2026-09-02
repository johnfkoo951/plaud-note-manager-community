from pathlib import Path

from core import locator
from core.locator import (
    Resource,
    make_uri,
    parse_uri,
    resource_for,
    transcripts_for,
    summaries_for,
    integrated_for,
)


# ---- make_uri / parse_uri round-trip -------------------------------------


def test_uri_round_trip_summary_with_extras() -> None:
    uri = make_uri("summary", "file-1", "claude", "default")

    assert uri == "plaud://summary/file-1/claude/default"
    assert parse_uri(uri) == ("summary", "file-1", ["claude", "default"])


def test_uri_round_trip_integrated_summary_with_extras() -> None:
    uri = make_uri("integrated-summary", "file-2", "gpt", "meeting")

    assert uri == "plaud://integrated-summary/file-2/gpt/meeting"
    assert parse_uri(uri) == ("integrated-summary", "file-2", ["gpt", "meeting"])


def test_uri_round_trip_plain_kind_no_extras() -> None:
    uri = make_uri("cmds-transcript", "file-3")

    assert uri == "plaud://cmds-transcript/file-3"
    assert parse_uri(uri) == ("cmds-transcript", "file-3", [])


# ---- parse_uri rejection cases -------------------------------------------


def test_parse_uri_rejects_non_plaud_scheme() -> None:
    assert parse_uri("https://summary/file-1") is None
    assert parse_uri("file-1/extra") is None


def test_parse_uri_rejects_single_segment() -> None:
    assert parse_uri("plaud://summary") is None


# ---- Resource.title_hint -------------------------------------------------


def test_title_hint_summary_with_model_and_template() -> None:
    r = Resource(
        uri=make_uri("summary", "file-1", "claude", "default"),
        path=Path("/tmp/x.md"),
        kind="summary",
        file_id="file-1",
        model="claude",
        template="default",
    )

    assert r.title_hint == "claude/default"


def test_title_hint_integrated_with_model_and_template() -> None:
    r = Resource(
        uri=make_uri("integrated-summary", "file-1", "gpt", "meeting"),
        path=Path("/tmp/x.md"),
        kind="integrated-summary",
        file_id="file-1",
        model="gpt",
        template="meeting",
    )

    assert r.title_hint == "gpt/meeting"


def test_title_hint_falls_back_to_kind_label_when_model_or_template_none() -> None:
    # model present, template missing -> fall back, NOT "claude/None"
    only_model = Resource(
        uri="plaud://integrated-summary/file-1",
        path=Path("/tmp/x.md"),
        kind="integrated-summary",
        file_id="file-1",
        model="claude",
        template=None,
    )
    assert only_model.title_hint == "integrated summary"
    assert only_model.title_hint != "claude/None"

    # both missing -> fall back, NOT "None/None"
    neither = Resource(
        uri="plaud://summary/file-1",
        path=Path("/tmp/x.md"),
        kind="summary",
        file_id="file-1",
        model=None,
        template=None,
    )
    assert neither.title_hint == "summary"
    assert neither.title_hint != "None/None"


def test_title_hint_plain_kind_uses_kind_label() -> None:
    r = Resource(
        uri="plaud://cmds-transcript/file-1",
        path=Path("/tmp/x.md"),
        kind="cmds-transcript",
        file_id="file-1",
    )

    assert r.title_hint == "cmds transcript"


# ---- resource_for --------------------------------------------------------


def test_resource_for_nonexistent_path_returns_none(tmp_path) -> None:
    missing = tmp_path / "does-not-exist.md"

    assert resource_for(missing, kind="summary", file_id="file-1") is None


def test_resource_for_real_file_populates_fields(tmp_path) -> None:
    path = tmp_path / "claude__default.md"
    path.write_text("# hello", encoding="utf-8")
    stat = path.stat()

    r = resource_for(
        path,
        kind="summary",
        file_id="file-1",
        model="claude",
        template="default",
    )

    assert r is not None
    assert r.uri == "plaud://summary/file-1/claude/default"
    assert r.kind == "summary"
    assert r.file_id == "file-1"
    assert r.model == "claude"
    assert r.template == "default"
    assert r.path == path.resolve()
    assert r.mtime == stat.st_mtime
    assert r.size == stat.st_size


# ---- transcripts_for / summaries_for / integrated_for --------------------


def test_transcripts_for_maps_known_filenames(monkeypatch, tmp_path) -> None:
    transcripts_dir = tmp_path / "transcripts"
    base = transcripts_dir / "file-1"
    base.mkdir(parents=True)
    (base / "plaud.transcript.md").write_text("t", encoding="utf-8")
    (base / "plaud.summary.md").write_text("s", encoding="utf-8")
    (base / "plaud.outline.md").write_text("o", encoding="utf-8")
    (base / "cmds.transcript.md").write_text("c", encoding="utf-8")
    # an unknown file is ignored
    (base / "stray.md").write_text("x", encoding="utf-8")

    monkeypatch.setattr(locator, "TRANSCRIPTS_DIR", transcripts_dir)

    resources = transcripts_for("file-1")

    by_kind = {r.kind: r for r in resources}
    assert set(by_kind) == {
        "plaud-transcript",
        "plaud-summary",
        "plaud-outline",
        "cmds-transcript",
    }
    cmds = by_kind["cmds-transcript"]
    assert cmds.uri == "plaud://cmds-transcript/file-1"
    assert cmds.model is None
    assert cmds.template is None


def test_transcripts_for_missing_dir_returns_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(locator, "TRANSCRIPTS_DIR", tmp_path / "transcripts")

    assert transcripts_for("nope") == []


def test_summaries_for_parses_model_and_template(monkeypatch, tmp_path) -> None:
    summaries_dir = tmp_path / "summaries"
    base = summaries_dir / "file-1"
    base.mkdir(parents=True)
    (base / "claude__default.md").write_text("a", encoding="utf-8")
    (base / "gpt__meeting.md").write_text("b", encoding="utf-8")

    monkeypatch.setattr(locator, "SUMMARIES_DIR", summaries_dir)

    resources = summaries_for("file-1")

    assert len(resources) == 2
    pairs = {(r.model, r.template) for r in resources}
    assert pairs == {("claude", "default"), ("gpt", "meeting")}
    for r in resources:
        assert r.kind == "summary"
        assert r.uri == f"plaud://summary/file-1/{r.model}/{r.template}"
        assert r.title_hint == f"{r.model}/{r.template}"


def test_integrated_for_parses_summary_and_transcript_suffixes(monkeypatch, tmp_path) -> None:
    integrated_dir = tmp_path / "integrated"
    base = integrated_dir / "file-1"
    base.mkdir(parents=True)
    (base / "claude__default.summary.md").write_text("s", encoding="utf-8")
    (base / "claude__default.transcript.md").write_text("t", encoding="utf-8")
    (base / "claude__default.md").write_text("a", encoding="utf-8")

    monkeypatch.setattr(locator, "INTEGRATED_DIR", integrated_dir)

    resources = integrated_for("file-1")

    by_kind = {r.kind: r for r in resources}
    assert set(by_kind) == {
        "integrated-summary",
        "integrated-transcript",
        "integrated-all",
    }
    for r in resources:
        assert r.model == "claude"
        assert r.template == "default"

    summary = by_kind["integrated-summary"]
    assert summary.uri == "plaud://integrated-summary/file-1/claude/default"
    assert summary.path.name == "claude__default.summary.md"

    transcript = by_kind["integrated-transcript"]
    assert transcript.path.name == "claude__default.transcript.md"
    assert transcript.uri == "plaud://integrated-transcript/file-1/claude/default"


def test_integrated_for_missing_dir_returns_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(locator, "INTEGRATED_DIR", tmp_path / "integrated")

    assert integrated_for("nope") == []
