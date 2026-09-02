"""Progressive-disclosure query API for Plaud notes.

Four layers, each strictly a superset of the previous:

    L0 peek   — filename, duration, folders, start_time/edit_time,
                has_content_cache, has_cmds_transcript, integrated_count, is_trash
    L1 brief  — + title, keywords, tags, vault_link counts by kind
    L2 outline — + Plaud auto-summary (first paragraph) + outline preview
    L3 deep   — + transcript / summary / integrated markdown bodies

Each call is cheap until you ask for the next layer. Designed so a query
agent can decide "this looks promising, give me L2" without doing a full
load up front.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, asdict, field
from typing import Any

from .locator import transcripts_for, integrated_for
from .storage import DEFAULT_DB


# ----- data classes -----------------------------------------------------


@dataclass
class L0Peek:
    file_id: str
    filename: str | None
    duration_ms: float | None
    edit_time: int | None
    start_time: int | None
    folders: list[str]
    is_trash: bool
    has_content_cache: bool
    has_cmds_transcript: bool
    integrated_count: int


@dataclass
class L1Brief(L0Peek):
    title: str | None = None
    description: str | None = None
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    vault_links: dict[str, int] = field(default_factory=dict)  # match_kind → count
    speakers: list[str] = field(default_factory=list)


@dataclass
class L2Outline(L1Brief):
    plaud_summary_preview: str | None = None
    plaud_outline_preview: str | None = None
    integrated_summary_preview: str | None = None


@dataclass
class L3Deep(L2Outline):
    plaud_transcript: str | None = None
    plaud_summary: str | None = None
    plaud_outline: str | None = None
    cmds_transcript: str | None = None
    integrated_summary: str | None = None
    integrated_transcript: str | None = None
    vault_link_details: list[dict] = field(default_factory=list)


# ----- helpers ----------------------------------------------------------


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DEFAULT_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _truncate(text: str | None, *, chars: int = 400) -> str | None:
    if not text:
        return None
    text = text.strip()
    return text[:chars] + ("…" if len(text) > chars else "")


# ----- L0 ----------------------------------------------------------------


def peek(file_id: str) -> L0Peek | None:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        if not row:
            return None
        folders = [
            r["name"]
            for r in conn.execute(
                "SELECT f.name FROM folders f "
                "JOIN file_folders ff ON ff.folder_id = f.id "
                "WHERE ff.file_id = ?",
                (file_id,),
            )
        ]
        has_content = (
            conn.execute("SELECT 1 FROM file_content WHERE file_id = ?", (file_id,)).fetchone()
            is not None
        )
        has_cmds = (
            conn.execute("SELECT 1 FROM cmds_transcripts WHERE file_id = ?", (file_id,)).fetchone()
            is not None
        )
        integrated_count = len(integrated_for(file_id))
        return L0Peek(
            file_id=file_id,
            filename=row["filename"],
            duration_ms=row["duration"],
            edit_time=row["edit_time"],
            start_time=row["start_time"],
            folders=folders,
            is_trash=bool(row["is_trash"]),
            has_content_cache=has_content,
            has_cmds_transcript=has_cmds,
            integrated_count=integrated_count,
        )
    finally:
        conn.close()


# ----- L1 ----------------------------------------------------------------


def brief(file_id: str) -> L1Brief | None:
    p = peek(file_id)
    if not p:
        return None
    conn = _conn()
    try:
        title = None
        description = None
        keywords: list[str] = []
        tags: list[str] = []
        speakers: list[str] = []

        crow = conn.execute(
            "SELECT title, keywords FROM file_content WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        if crow:
            title = crow["title"]
            try:
                keywords = json.loads(crow["keywords"] or "[]")
            except Exception:
                keywords = []

        # note_metadata extends with description + manual tags
        # (table created by storage; guard against absence)
        try:
            nrow = conn.execute(
                "SELECT title, description FROM note_metadata WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if nrow:
                title = nrow["title"] or title
                description = nrow["description"] or description
        except sqlite3.OperationalError:
            pass

        try:
            tag_rows = conn.execute(
                "SELECT tag FROM note_tags WHERE file_id = ? ORDER BY tag",
                (file_id,),
            ).fetchall()
            tags = [r["tag"] for r in tag_rows]
        except sqlite3.OperationalError:
            pass

        # vault link summary
        vault_links: dict[str, int] = {}
        try:
            for row in conn.execute(
                "SELECT match_kind, COUNT(*) AS n FROM vault_links "
                "WHERE file_id = ? GROUP BY match_kind",
                (file_id,),
            ):
                vault_links[row["match_kind"]] = row["n"]
        except sqlite3.OperationalError:
            pass

        # speakers from CMDS transcript
        try:
            crow = conn.execute(
                "SELECT segments FROM cmds_transcripts WHERE file_id = ? "
                "ORDER BY fetched_at DESC LIMIT 1",
                (file_id,),
            ).fetchone()
            if crow:
                segs = json.loads(crow["segments"] or "[]")
                seen: list[str] = []
                for s in segs:
                    sp = s.get("speaker")
                    if sp and sp not in seen:
                        seen.append(sp)
                speakers = seen
        except Exception:
            pass

        return L1Brief(
            **asdict(p),
            title=title,
            description=description,
            keywords=keywords,
            tags=tags,
            vault_links=vault_links,
            speakers=speakers,
        )
    finally:
        conn.close()


# ----- L2 ----------------------------------------------------------------


def outline(file_id: str) -> L2Outline | None:
    b = brief(file_id)
    if not b:
        return None
    conn = _conn()
    try:
        plaud_summary = None
        plaud_outline = None
        crow = conn.execute(
            "SELECT summary_md, outline FROM file_content WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        if crow:
            plaud_summary = crow["summary_md"]
            try:
                items = json.loads(crow["outline"] or "[]")
                plaud_outline = "\n".join(f"- {it.get('topic', '')}" for it in items)
            except Exception:
                plaud_outline = None

        # try to read first integrated summary if any
        integrated_preview = None
        for r in integrated_for(file_id):
            if r.kind == "integrated-summary":
                try:
                    integrated_preview = r.read_text()
                except Exception:
                    integrated_preview = None
                break

        return L2Outline(
            **asdict(b),
            plaud_summary_preview=_truncate(plaud_summary, chars=400),
            plaud_outline_preview=_truncate(plaud_outline, chars=600),
            integrated_summary_preview=_truncate(integrated_preview, chars=600),
        )
    finally:
        conn.close()


# ----- L3 ----------------------------------------------------------------


def deep(file_id: str) -> L3Deep | None:
    o = outline(file_id)
    if not o:
        return None
    # Pull full bodies straight from disk via locator
    transcripts = {r.kind: r for r in transcripts_for(file_id)}
    integrated = {r.kind: r for r in integrated_for(file_id)}

    def _maybe_read(r) -> str | None:
        try:
            return r.read_text() if r else None
        except Exception:
            return None

    conn = _conn()
    try:
        plaud_summary = None
        try:
            crow = conn.execute(
                "SELECT summary_md FROM file_content WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            plaud_summary = crow["summary_md"] if crow else None
        except sqlite3.OperationalError:
            pass

        # vault link details
        try:
            rows = conn.execute(
                "SELECT vl.match_kind, vl.keyword, vl.confidence, "
                "       v.title, v.vault, v.rel_path, v.path, v.aliases, v.tags "
                "  FROM vault_links vl "
                "  JOIN vault_notes v ON v.id = vl.vault_note_id "
                " WHERE vl.file_id = ? "
                " ORDER BY vl.confidence DESC",
                (file_id,),
            ).fetchall()
            link_details = [dict(row) for row in rows]
        except sqlite3.OperationalError:
            link_details = []

        return L3Deep(
            **asdict(o),
            plaud_transcript=_maybe_read(transcripts.get("plaud-transcript")),
            plaud_summary=plaud_summary,
            plaud_outline=_maybe_read(transcripts.get("plaud-outline")),
            cmds_transcript=_maybe_read(transcripts.get("cmds-transcript")),
            integrated_summary=_maybe_read(integrated.get("integrated-summary")),
            integrated_transcript=_maybe_read(integrated.get("integrated-transcript")),
            vault_link_details=link_details,
        )
    finally:
        conn.close()


# ----- search API -------------------------------------------------------


def search(
    *,
    keyword: str | None = None,
    tag: str | None = None,
    folder: str | None = None,
    vault_note_title: str | None = None,
    limit: int = 50,
) -> list[L1Brief]:
    """Find file_ids matching any combination of filters, return L1Brief list.

    All filters are AND-ed. `keyword` matches Plaud keywords OR our normalized
    keyword vocabulary. `vault_note_title` finds files linked to that note via
    vault_links.
    """
    conn = _conn()
    try:
        clauses: list[str] = []
        params: list[Any] = []
        joins: list[str] = []
        if keyword:
            joins.append(
                "JOIN file_keywords fk_q ON fk_q.file_id = f.id "
                "JOIN keywords k_q ON k_q.id = fk_q.keyword_id AND k_q.term = ? COLLATE NOCASE"
            )
            params.append(keyword)
        if tag:
            joins.append("JOIN note_tags nt_q ON nt_q.file_id = f.id AND nt_q.tag = ?")
            params.append(tag)
        if folder:
            joins.append(
                "JOIN file_folders ff_q ON ff_q.file_id = f.id "
                "JOIN folders fl_q ON fl_q.id = ff_q.folder_id AND fl_q.name = ?"
            )
            params.append(folder)
        if vault_note_title:
            joins.append(
                "JOIN vault_links vl_q ON vl_q.file_id = f.id "
                "JOIN vault_notes vn_q ON vn_q.id = vl_q.vault_note_id "
                "AND vn_q.title = ? COLLATE NOCASE"
            )
            params.append(vault_note_title)
        clauses.append("f.is_trash = 0")

        sql = (
            "SELECT DISTINCT f.id FROM files f "
            + " ".join(joins)
            + " WHERE "
            + " AND ".join(clauses)
            + " ORDER BY COALESCE(f.start_time, f.edit_time * 1000) DESC "
            + f" LIMIT {int(limit)}"
        )
        try:
            ids = [row[0] for row in conn.execute(sql, params)]
        except sqlite3.OperationalError:
            # keyword/vault-note filters join tables only vault_index creates;
            # treat their absence as "no matches" rather than crashing.
            ids = []
    finally:
        conn.close()
    return [b for b in (brief(i) for i in ids) if b is not None]
