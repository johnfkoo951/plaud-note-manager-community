"""Canonical resource locators for the local Plaud data.

Goal: any external pipeline (embedding, search, Obsidian sync, RAG) should be
able to enumerate, address, and read our local notes without knowing about
our internal directory structure.

Each resource has:
  - `uri`     stable string id of the form  plaud://{kind}/{file_id}[/{model}/{template}]
  - `path`    absolute filesystem Path
  - `kind`    one of plaud-transcript / plaud-summary / plaud-outline /
              cmds-transcript / summary / integrated-summary / integrated-transcript
  - `file_id` Plaud immutable id (= group key)
  - `mtime`   for incremental embedding

`iter_resources()` walks the entire local store and yields every readable
markdown / json artifact. Designed for `for r in iter_resources(): embed(r.path.read_text())`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator

from .paths import (
    INTEGRATED_DIR,
    SUMMARIES_DIR,
    TRANSCRIPTS_DIR,
    DATA_DIR,
    _component,
)
from .storage import DEFAULT_DB


# ---- URI scheme ----------------------------------------------------------

URI_PREFIX = "plaud://"


def make_uri(kind: str, file_id: str, *extras: str) -> str:
    parts = [
        _component(kind, "resource kind"),
        _component(file_id, "file_id"),
        *(_component(value, "resource component") for value in extras),
    ]
    return URI_PREFIX + "/".join(parts)


def parse_uri(uri: str) -> tuple[str, str, list[str]] | None:
    if not uri.startswith(URI_PREFIX):
        return None
    parts = uri[len(URI_PREFIX) :].split("/")
    if len(parts) < 2:
        return None
    kind, file_id, *extras = parts
    try:
        clean_kind = _component(kind, "resource kind")
        clean_file_id = _component(file_id, "file_id")
        clean_extras = [_component(value, "resource component") for value in extras]
    except ValueError:
        return None
    return clean_kind, clean_file_id, clean_extras


# ---- Resource record -----------------------------------------------------


@dataclass(frozen=True)
class Resource:
    uri: str
    path: Path
    kind: str
    file_id: str
    model: str | None = None
    template: str | None = None
    mtime: float = 0.0
    size: int = 0

    @property
    def title_hint(self) -> str:
        """Best-effort human title used for embedding metadata.

        For ``summary`` and ``integrated*`` kinds the two URI extras are
        ``(model, template)``; other kinds carry none. Falls back to the
        kind label when model/template are absent (avoids "None/None").
        """
        if (self.kind.startswith("integrated") or self.kind == "summary") and (
            self.model and self.template
        ):
            return f"{self.model}/{self.template}"
        return self.kind.replace("-", " ")

    def read_text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["path"] = str(self.path)
        return d


def resource_for(
    path: Path, *, kind: str, file_id: str, model: str | None = None, template: str | None = None
) -> Resource | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    extras: list[str] = []
    if model:
        extras.append(model)
    if template:
        extras.append(template)
    return Resource(
        uri=make_uri(kind, file_id, *extras),
        path=path.resolve(),
        kind=kind,
        file_id=file_id,
        model=model,
        template=template,
        mtime=stat.st_mtime,
        size=stat.st_size,
    )


# ---- Per-file lookup -----------------------------------------------------


def transcripts_for(file_id: str) -> list[Resource]:
    """All transcript markdown files we keep on disk for one Plaud file."""
    out: list[Resource] = []
    base = TRANSCRIPTS_DIR / _component(file_id, "file_id")
    if not base.exists():
        return out
    mapping = {
        "plaud.transcript.md": "plaud-transcript",
        "plaud.summary.md": "plaud-summary",
        "plaud.outline.md": "plaud-outline",
        "cmds.transcript.md": "cmds-transcript",
    }
    for filename, kind in mapping.items():
        r = resource_for(base / filename, kind=kind, file_id=file_id)
        if r:
            out.append(r)
    return out


def summaries_for(file_id: str) -> list[Resource]:
    """Single-source AI summaries ({model}__{template}.md)."""
    out: list[Resource] = []
    base = SUMMARIES_DIR / _component(file_id, "file_id")
    if not base.exists():
        return out
    for path in sorted(base.glob("*.md")):
        model, _, template = path.stem.partition("__")
        r = resource_for(path, kind="summary", file_id=file_id, model=model, template=template)
        if r:
            out.append(r)
    return out


def integrated_for(file_id: str) -> list[Resource]:
    """Integrated pipeline outputs (summary + cleaned transcript)."""
    out: list[Resource] = []
    base = INTEGRATED_DIR / _component(file_id, "file_id")
    if not base.exists():
        return out
    for path in sorted(base.glob("*.md")):
        stem = path.stem  # may end in .summary or .transcript
        if path.name.endswith(".summary.md"):
            stem_clean = stem[: -len(".summary")]
            kind = "integrated-summary"
        elif path.name.endswith(".transcript.md"):
            stem_clean = stem[: -len(".transcript")]
            kind = "integrated-transcript"
        else:
            stem_clean = stem
            kind = "integrated-all"
        model, _, template = stem_clean.partition("__")
        r = resource_for(path, kind=kind, file_id=file_id, model=model, template=template)
        if r:
            out.append(r)
    return out


def resources_for(file_id: str) -> list[Resource]:
    """Every locally-stored markdown belonging to one Plaud file."""
    return transcripts_for(file_id) + summaries_for(file_id) + integrated_for(file_id)


# ---- Global enumeration --------------------------------------------------


def known_file_ids() -> list[str]:
    """All file_ids that have at least one local artifact OR a metadata row."""
    ids: set[str] = set()
    for parent in (TRANSCRIPTS_DIR, SUMMARIES_DIR, INTEGRATED_DIR):
        if parent.exists():
            ids.update(p.name for p in parent.iterdir() if p.is_dir())
    if DEFAULT_DB.exists():
        conn = sqlite3.connect(DEFAULT_DB)
        try:
            ids.update(row[0] for row in conn.execute("SELECT id FROM files"))
        finally:
            conn.close()
    return sorted(ids)


def iter_resources(*, since_mtime: float = 0.0) -> Iterator[Resource]:
    """Yield every readable artifact across all files, newest first.

    Pass `since_mtime` to do incremental embedding.
    """
    for file_id in known_file_ids():
        for r in resources_for(file_id):
            if r.mtime >= since_mtime:
                yield r


# ---- SQLite-backed metadata for richer embeddings -----------------------


def file_metadata(file_id: str) -> dict:
    """Lightweight metadata snapshot: filename, folders, tags, title, keywords.

    Useful as embedding-metadata so the vector store knows what the chunk is.
    """
    out: dict = {"file_id": file_id}
    if not DEFAULT_DB.exists():
        return out
    conn = sqlite3.connect(DEFAULT_DB)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT filename, duration, start_time, edit_time, is_trash FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        if row:
            out.update({k: row[k] for k in row.keys()})

        crow = conn.execute(
            "SELECT title, keywords FROM file_content WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        if crow:
            out["title"] = crow["title"]
            try:
                out["keywords"] = json.loads(crow["keywords"] or "[]")
            except Exception:
                out["keywords"] = []

        folders = conn.execute(
            "SELECT f.name FROM folders f "
            "JOIN file_folders ff ON ff.folder_id = f.id "
            "WHERE ff.file_id = ?",
            (file_id,),
        ).fetchall()
        out["folders"] = [r["name"] for r in folders]

        tags = conn.execute("SELECT tag FROM note_tags WHERE file_id = ?", (file_id,)).fetchall()
        out["tags"] = [r["tag"] for r in tags]
    finally:
        conn.close()
    return out


# ---- Manifest export -----------------------------------------------------


def build_manifest() -> dict:
    """JSON-serializable index of every local resource.

    Drop this onto disk and an external pipeline can plan embedding work
    without scanning the filesystem itself.
    """
    items = []
    for r in iter_resources():
        item = r.to_dict()
        item["metadata"] = file_metadata(r.file_id)
        items.append(item)
    return {
        "data_dir": str(DATA_DIR),
        "uri_prefix": URI_PREFIX,
        "count": len(items),
        "items": items,
    }
