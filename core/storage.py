"""SQLite-backed metadata + content cache for Plaud files."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import FileContent, FileStatus, Folder, PlaudFile
from .paths import DATA_DIR
from .tags import normalize_tags

DEFAULT_DB = DATA_DIR / "plaud.db"

# Bump when SCHEMA_TABLES/_migrate change; gates the migration fast-path.
SCHEMA_VERSION = 2


def _tighten_private_file(path: Path, mode: int) -> None:
    """Apply POSIX privacy bits where they exist.

    Windows protection comes from the current user's LocalAppData ACL; chmod's
    read-only emulation there is neither useful nor equivalent to a Unix mode.
    """

    if os.name == "posix":
        os.chmod(path, mode)


SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS files (
    id          TEXT PRIMARY KEY,
    filename    TEXT,
    filesize    INTEGER,
    duration    REAL,
    edit_time   INTEGER,
    start_time  INTEGER,
    is_trash    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'new',
    local_path  TEXT,
    -- Local-only reading state; upsert_file never touches these, so they
    -- survive cloud syncs. seen_at NULL = the user has not opened it yet.
    seen_at     INTEGER,
    starred     INTEGER NOT NULL DEFAULT 0,
    synced_at   INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS folders (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    icon    TEXT,
    color   TEXT,
    synced_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS file_folders (
    file_id   TEXT NOT NULL,
    folder_id TEXT NOT NULL,
    PRIMARY KEY (file_id, folder_id)
);
CREATE TABLE IF NOT EXISTS file_content (
    file_id      TEXT PRIMARY KEY,
    title        TEXT,
    transcript   TEXT,
    outline      TEXT,
    summary_md   TEXT,
    summary_extra TEXT,
    keywords     TEXT,
    fetched_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cmds_transcripts (
    file_id     TEXT NOT NULL,
    model       TEXT NOT NULL,
    language    TEXT,
    text        TEXT,
    segments    TEXT,
    fetched_at  INTEGER NOT NULL,
    PRIMARY KEY (file_id, model)
);
CREATE TABLE IF NOT EXISTS speakers (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL UNIQUE,
    is_self   INTEGER NOT NULL DEFAULT 0,
    notes     TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS note_metadata (
    file_id         TEXT PRIMARY KEY,
    title           TEXT,
    description     TEXT,
    note_type       TEXT,
    status          TEXT NOT NULL DEFAULT 'unread',
    usage_status    TEXT NOT NULL DEFAULT 'unused',
    category        TEXT,
    folder_id       TEXT,
    folder_name     TEXT,
    vault_path      TEXT,
    draft_path      TEXT,
    final_note_path TEXT,
    metadata_json   TEXT,
    generated_at    INTEGER,
    updated_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS note_tags (
    file_id    TEXT NOT NULL,
    tag        TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'manual',
    created_at INTEGER NOT NULL,
    PRIMARY KEY (file_id, tag)
);
CREATE TABLE IF NOT EXISTS note_reuse (
    file_id    TEXT NOT NULL,
    channel    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'flagged',
    note       TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (file_id, channel)
);
CREATE TABLE IF NOT EXISTS dual_pipeline (
    file_id          TEXT PRIMARY KEY,
    status           TEXT NOT NULL DEFAULT 'marked',
    speaker_proposal TEXT,
    speaker_map      TEXT,
    vault_path       TEXT,
    error            TEXT,
    updated_at       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS note_references (
    file_id    TEXT NOT NULL,
    path       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    title      TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (file_id, path)
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS files_status_idx ON files(status);
CREATE INDEX IF NOT EXISTS files_edit_time_idx ON files(edit_time DESC);
CREATE INDEX IF NOT EXISTS files_trash_idx ON files(is_trash);
CREATE INDEX IF NOT EXISTS note_tags_tag_idx ON note_tags(tag);
CREATE INDEX IF NOT EXISTS note_refs_file_idx ON note_references(file_id);
CREATE INDEX IF NOT EXISTS file_folders_folder_idx ON file_folders(folder_id);
CREATE INDEX IF NOT EXISTS note_reuse_channel_idx ON note_reuse(channel, status);
"""


class Storage:
    def __init__(self, db_path: Path = DEFAULT_DB) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _tighten_private_file(db_path.parent, 0o700)
        self._db_path = db_path
        self._fts_ok = False
        with self._connect() as conn:
            conn.executescript(SCHEMA_TABLES)
            self._migrate(conn)
            conn.executescript(SCHEMA_INDEXES)
            self._fts_ok = self._ensure_search_index(conn)

    # ---------- full-content search (FTS5 trigram — Korean substring OK) ----------

    def _ensure_search_index(self, conn: sqlite3.Connection) -> bool:
        """Create the recording FTS table and backfill it once. Returns False
        (search disabled) if this SQLite build lacks FTS5/trigram."""
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS recording_fts "
                "USING fts5(file_id UNINDEXED, title, body, tokenize='trigram')"
            )
        except sqlite3.OperationalError:
            return False
        fts_n = conn.execute("SELECT COUNT(*) FROM recording_fts").fetchone()[0]
        content_n = conn.execute("SELECT COUNT(*) FROM file_content").fetchone()[0]
        if fts_n == 0 and content_n > 0:
            self._backfill_search_index(conn)
        return True

    @staticmethod
    def _transcript_text(transcript_json: str | None) -> str:
        if not transcript_json:
            return ""
        try:
            segs = json.loads(transcript_json)
        except (TypeError, json.JSONDecodeError):
            return ""
        return " ".join(str(s.get("content", "")) for s in segs if isinstance(s, dict))

    def _index_one(
        self, conn: sqlite3.Connection, file_id: str, title, transcript, summary
    ) -> None:
        body = "\n".join(
            part for part in (self._transcript_text(transcript), summary or "") if part
        )
        conn.execute("DELETE FROM recording_fts WHERE file_id = ?", (file_id,))
        conn.execute(
            "INSERT INTO recording_fts (file_id, title, body) VALUES (?, ?, ?)",
            (file_id, title or "", body),
        )

    def _backfill_search_index(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT file_id, title, transcript, summary_md FROM file_content"
        ).fetchall()
        for r in rows:
            self._index_one(conn, r["file_id"], r["title"], r["transcript"], r["summary_md"])

    def rebuild_search_index(self) -> int:
        """Drop and rebuild the FTS index from scratch. Returns rows indexed."""
        with self._connect() as conn:
            if not self._ensure_search_index(conn):
                return 0
            conn.execute("DELETE FROM recording_fts")
            self._backfill_search_index(conn)
            return conn.execute("SELECT COUNT(*) FROM recording_fts").fetchone()[0]

    def search_recordings(self, query: str, *, limit: int = 200) -> list[dict]:
        """Full-content search over title + transcript + summary. Returns
        [{file_id, snippet}] ordered by relevance. Uses trigram FTS5 when every
        term is >=3 chars (fast); otherwise falls back to a LIKE scan so short
        Korean terms (2 chars) still match."""
        terms = [t for t in query.split() if t.strip()]
        if not terms:
            return []
        with self._connect() as conn:
            if self._fts_ok and all(len(t) >= 3 for t in terms):
                hits = self._search_fts(conn, terms, limit)
                if hits:
                    return hits
            return self._search_like(conn, terms, limit)

    def _search_fts(self, conn, terms: list[str], limit: int) -> list[dict]:
        match = " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)
        try:
            rows = conn.execute(
                """
                SELECT file_id,
                       snippet(recording_fts, 2, '«', '»', '…', 12) AS snippet
                  FROM recording_fts
                 WHERE recording_fts MATCH ?
                 ORDER BY bm25(recording_fts, 10.0, 1.0)
                 LIMIT ?
                """,
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [{"file_id": r["file_id"], "snippet": r["snippet"] or ""} for r in rows]

    def _search_like(self, conn, terms: list[str], limit: int) -> list[dict]:
        where = " AND ".join(
            "(title LIKE ? OR summary_md LIKE ? OR transcript LIKE ?)" for _ in terms
        )
        params: list = []
        for t in terms:
            like = f"%{t}%"
            params += [like, like, like]
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT file_id, title, summary_md
              FROM file_content
             WHERE {where}
             LIMIT ?
            """,
            params,
        ).fetchall()
        out = []
        for r in rows:
            text = r["summary_md"] or r["title"] or ""
            out.append({"file_id": r["file_id"], "snippet": text[:120]})
        return out

    def _migrate(self, conn: sqlite3.Connection) -> None:
        # Version-gate the (idempotent) migrations so an up-to-date DB skips the
        # PRAGMA table_info diffing on every open.
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        cols = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
        if "is_trash" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN is_trash INTEGER NOT NULL DEFAULT 0")
        if "start_time" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN start_time INTEGER")
        if "seen_at" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN seen_at INTEGER")
            # The pre-existing library counts as "seen" — only recordings that
            # arrive after this migration should light up as unread.
            conn.execute("UPDATE files SET seen_at = strftime('%s','now')")
        if "starred" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN starred INTEGER NOT NULL DEFAULT 0")
        note_cols = {row[1] for row in conn.execute("PRAGMA table_info(note_metadata)")}
        if note_cols:
            if "usage_status" not in note_cols:
                conn.execute(
                    "ALTER TABLE note_metadata ADD COLUMN usage_status TEXT "
                    "NOT NULL DEFAULT 'unused'"
                )
            if "category" not in note_cols:
                conn.execute("ALTER TABLE note_metadata ADD COLUMN category TEXT")
            if "folder_id" not in note_cols:
                conn.execute("ALTER TABLE note_metadata ADD COLUMN folder_id TEXT")
            if "folder_name" not in note_cols:
                conn.execute("ALTER TABLE note_metadata ADD COLUMN folder_name TEXT")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        _tighten_private_file(self._db_path, 0o600)
        conn.row_factory = sqlite3.Row
        # WAL mode lets Swift readers/writers and Python writers share the
        # database concurrently without locking each other out.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # Wait (instead of failing immediately) when another writer holds the lock.
        conn.execute("PRAGMA busy_timeout=5000")
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self._db_path}{suffix}")
            if sidecar.exists():
                _tighten_private_file(sidecar, 0o600)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            for suffix in ("", "-wal", "-shm"):
                path = Path(f"{self._db_path}{suffix}")
                if path.exists():
                    _tighten_private_file(path, 0o600)

    # ---------- files ----------

    def upsert_file(self, file: PlaudFile, *, now: int, is_trash: int = 0) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO files (id, filename, filesize, duration, edit_time,
                                   start_time, is_trash, status, synced_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    filename   = excluded.filename,
                    filesize   = excluded.filesize,
                    duration   = excluded.duration,
                    edit_time  = excluded.edit_time,
                    start_time = excluded.start_time,
                    is_trash   = excluded.is_trash,
                    synced_at  = excluded.synced_at,
                    updated_at = excluded.updated_at
                """,
                (
                    file.id,
                    file.filename or file.fullname,
                    file.filesize,
                    file.duration,
                    file.edit_time,
                    file.start_time,
                    is_trash,
                    now,
                    now,
                ),
            )

    def mark_downloaded(self, file_id: str, local_path: Path, *, now: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE files SET status = ?, local_path = ?, updated_at = ?
                    WHERE id = ?""",
                (FileStatus.DOWNLOADED.value, str(local_path), now, file_id),
            )

    def files_by_status(self, status: FileStatus) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM files WHERE status = ? ORDER BY edit_time DESC",
                    (status.value,),
                )
            )

    def get_file_row(self, file_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()

    def mark_seen(self, file_id: str, *, now: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE files SET seen_at = ? WHERE id = ? AND seen_at IS NULL",
                (now, file_id),
            )

    def set_starred(self, file_id: str, starred: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE files SET starred = ? WHERE id = ?",
                (1 if starred else 0, file_id),
            )

    # Cheap id-set accessors for derived progress (core/progress.py).

    def active_file_ids(self) -> set[str]:
        with self._connect() as conn:
            return {r[0] for r in conn.execute("SELECT id FROM files WHERE is_trash = 0")}

    def cached_file_ids(self) -> set[str]:
        with self._connect() as conn:
            return {r[0] for r in conn.execute("SELECT file_id FROM file_content")}

    def cmds_transcribed_file_ids(self) -> set[str]:
        with self._connect() as conn:
            return {r[0] for r in conn.execute("SELECT DISTINCT file_id FROM cmds_transcripts")}

    def counts(self) -> dict[str, Any]:
        """Library summary counts for the status dashboard."""
        with self._connect() as conn:

            def one(sql: str) -> int:
                return int(conn.execute(sql).fetchone()[0])

            usage = {
                row[0]: int(row[1])
                for row in conn.execute(
                    "SELECT usage_status, COUNT(*) FROM note_metadata GROUP BY usage_status"
                )
            }
            return {
                "total": one("SELECT COUNT(*) FROM files WHERE is_trash = 0"),
                "trash": one("SELECT COUNT(*) FROM files WHERE is_trash = 1"),
                "unfiled": one(
                    "SELECT COUNT(*) FROM files WHERE is_trash = 0 "
                    "AND id NOT IN (SELECT file_id FROM file_folders)"
                ),
                "folders": one("SELECT COUNT(*) FROM folders"),
                "cached": one("SELECT COUNT(*) FROM file_content"),
                "usage_status": usage,
            }

    # ---------- folders ----------

    def replace_folders(self, folders: list[Folder], *, now: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM folders")
            conn.executemany(
                "INSERT INTO folders (id, name, icon, color, synced_at) VALUES (?, ?, ?, ?, ?)",
                [(f.id, f.name, f.icon, f.color, now) for f in folders],
            )
            # Drop file->folder links whose folder no longer exists, so downstream
            # classification/link guards aren't poisoned by orphaned rows.
            conn.execute("DELETE FROM file_folders WHERE folder_id NOT IN (SELECT id FROM folders)")

    def set_file_folders(self, file_id: str, folder_ids: list[str]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM file_folders WHERE file_id = ?", (file_id,))
            conn.executemany(
                "INSERT INTO file_folders (file_id, folder_id) VALUES (?, ?)",
                [(file_id, fid) for fid in folder_ids],
            )

    def files_with_multiple_folders(self) -> list[tuple[str, list[str]]]:
        """Files whose local mapping still holds >1 folder (breaks Plaud web)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT file_id, GROUP_CONCAT(folder_id, char(31)) AS ids
                  FROM file_folders
                 GROUP BY file_id
                HAVING COUNT(*) > 1
                """
            ).fetchall()
        return [(r["file_id"], r["ids"].split(chr(31))) for r in rows]

    def set_file_name(self, file_id: str, name: str, *, now: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE files SET filename = ?, updated_at = ? WHERE id = ?",
                (name, now, file_id),
            )

    def list_folders(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(conn.execute("SELECT * FROM folders ORDER BY name COLLATE NOCASE"))

    def folder_by_name(self, name: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM folders WHERE lower(name) = lower(?) LIMIT 1",
                (name,),
            ).fetchone()

    # ---------- content ----------

    def save_content(self, content: FileContent, *, now: int) -> None:
        # Never persist an empty Plaud result (still-processing recording) — it
        # would create a stale "no content" cache that never self-heals. Folder
        # assignment is independent of content, so still sync that.
        if content.is_empty:
            self.set_file_folders(content.file_id, content.folder_ids)
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO file_content
                    (file_id, title, transcript, outline, summary_md,
                     summary_extra, keywords, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    title = excluded.title,
                    transcript = excluded.transcript,
                    outline = excluded.outline,
                    summary_md = excluded.summary_md,
                    summary_extra = excluded.summary_extra,
                    keywords = excluded.keywords,
                    fetched_at = excluded.fetched_at
                """,
                (
                    content.file_id,
                    content.title,
                    json.dumps([s.model_dump() for s in content.transcript], ensure_ascii=False),
                    json.dumps([o.model_dump() for o in content.outline], ensure_ascii=False),
                    content.summary_md,
                    json.dumps(content.summary_extra_md, ensure_ascii=False),
                    json.dumps(content.keywords, ensure_ascii=False),
                    now,
                ),
            )
            if self._fts_ok:
                transcript_json = json.dumps(
                    [s.model_dump() for s in content.transcript], ensure_ascii=False
                )
                self._index_one(
                    conn, content.file_id, content.title, transcript_json, content.summary_md
                )
        self.set_file_folders(content.file_id, content.folder_ids)

    def delete_empty_content(self) -> list[str]:
        """Drop stale empty caches (no transcript/summary/outline) so they
        re-fetch. Returns the cleared file ids."""
        with self._connect() as conn:
            ids = [
                r[0]
                for r in conn.execute(
                    """
                    SELECT file_id FROM file_content
                     WHERE COALESCE(length(transcript), 0) <= 2
                       AND COALESCE(length(summary_md), 0) = 0
                       AND COALESCE(length(outline), 0) <= 2
                    """
                ).fetchall()
            ]
            for fid in ids:
                conn.execute("DELETE FROM file_content WHERE file_id = ?", (fid,))
                if self._fts_ok:
                    conn.execute("DELETE FROM recording_fts WHERE file_id = ?", (fid,))
        return ids

    def get_content_row(self, file_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM file_content WHERE file_id = ?", (file_id,)
            ).fetchone()

    def files_without_content(self) -> list[sqlite3.Row]:
        """Files that don't yet have transcript/summary cached."""
        with self._connect() as conn:
            return list(
                conn.execute("""
                SELECT id FROM files
                 WHERE is_trash = 0
                   AND id NOT IN (SELECT file_id FROM file_content)
                 ORDER BY edit_time DESC
            """)
            )

    # ---------- note metadata / tags ----------

    def upsert_note_metadata(
        self,
        *,
        file_id: str,
        now: int,
        title: str | None = None,
        description: str | None = None,
        note_type: str | None = None,
        status: str | None = None,
        usage_status: str | None = None,
        category: str | None = None,
        folder_id: str | None = None,
        folder_name: str | None = None,
        vault_path: Path | str | None = None,
        draft_path: Path | str | None = None,
        final_note_path: Path | str | None = None,
        metadata: dict | None = None,
        generated_at: int | None = None,
    ) -> None:
        """Insert or update note metadata.

        For every COALESCE'd column, passing None means "leave unchanged" on an
        existing row (the new value only applies when non-None). Use
        update_usage_status / update_note_folder for explicit overwrites.
        """
        metadata_json = (
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True)
            if metadata is not None
            else None
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO note_metadata
                    (file_id, title, description, note_type, status, usage_status,
                     category, folder_id, folder_name, vault_path, draft_path,
                     final_note_path, metadata_json, generated_at, updated_at)
                VALUES (?, ?, ?, ?, COALESCE(?, 'unread'), COALESCE(?, 'unused'),
                        ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    title = COALESCE(excluded.title, title),
                    description = COALESCE(excluded.description, description),
                    note_type = COALESCE(excluded.note_type, note_type),
                    status = COALESCE(excluded.status, status),
                    usage_status = COALESCE(excluded.usage_status, usage_status),
                    category = COALESCE(excluded.category, category),
                    folder_id = COALESCE(excluded.folder_id, folder_id),
                    folder_name = COALESCE(excluded.folder_name, folder_name),
                    vault_path = COALESCE(excluded.vault_path, vault_path),
                    draft_path = COALESCE(excluded.draft_path, draft_path),
                    final_note_path = COALESCE(excluded.final_note_path, final_note_path),
                    metadata_json = COALESCE(excluded.metadata_json, metadata_json),
                    generated_at = COALESCE(excluded.generated_at, generated_at),
                    updated_at = excluded.updated_at
                """,
                (
                    file_id,
                    title,
                    description,
                    note_type,
                    status,
                    usage_status,
                    category,
                    folder_id,
                    folder_name,
                    str(vault_path) if vault_path else None,
                    str(draft_path) if draft_path else None,
                    str(final_note_path) if final_note_path else None,
                    metadata_json,
                    generated_at,
                    now,
                ),
            )

    def update_note_folder(
        self,
        file_id: str,
        *,
        folder_id: str | None,
        folder_name: str | None,
        now: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE note_metadata
                   SET folder_id = COALESCE(?, folder_id),
                       folder_name = COALESCE(?, folder_name),
                       updated_at = ?
                 WHERE file_id = ?
                """,
                (folder_id, folder_name, now, file_id),
            )

    def update_usage_status(self, file_id: str, usage_status: str, *, now: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO note_metadata (file_id, status, usage_status, updated_at)
                VALUES (?, 'unread', ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    usage_status = excluded.usage_status,
                    updated_at = excluded.updated_at
                """,
                (file_id, usage_status, now),
            )

    def files_for_classification(
        self,
        *,
        include_filed: bool = False,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        where = "f.is_trash = 0"
        if not include_filed:
            where += " AND f.id NOT IN (SELECT file_id FROM file_folders)"
        sql = f"""
            SELECT f.*, fc.title, fc.keywords, fc.summary_md, fc.summary_extra
              FROM files f
              LEFT JOIN file_content fc ON fc.file_id = f.id
             WHERE {where}
             ORDER BY COALESCE(f.start_time, f.edit_time * 1000) DESC
        """
        params: list[int] = []
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            return list(conn.execute(sql, params))

    def files_for_auto_metadata(
        self,
        *,
        fetched_after: int | None = None,
    ) -> list[sqlite3.Row]:
        """Candidates for automatic metadata generation.

        Cheap SQL prefilter only: has transcript/summary content, and either
        no successful generation yet or content refetched since. The caller
        does the precise source-hash / backoff filtering in Python.
        """
        sql = """
            SELECT f.id AS file_id, fc.transcript, fc.summary_md,
                   fc.summary_extra, fc.fetched_at,
                   nm.generated_at, nm.metadata_json
              FROM files f
              JOIN file_content fc ON fc.file_id = f.id
              LEFT JOIN note_metadata nm ON nm.file_id = f.id
             WHERE f.is_trash = 0
               AND (COALESCE(fc.transcript, '') != ''
                    OR COALESCE(fc.summary_md, '') != '')
               AND (nm.generated_at IS NULL OR fc.fetched_at > nm.generated_at)
        """
        params: list[int] = []
        if fetched_after is not None:
            sql += " AND fc.fetched_at >= ?"
            params.append(int(fetched_after))
        sql += " ORDER BY fc.fetched_at DESC"
        with self._connect() as conn:
            return list(conn.execute(sql, params))

    # ---------- reuse marks (content-repurposing checks) ----------

    def set_reuse(
        self,
        file_id: str,
        channel: str,
        *,
        status: str = "flagged",
        note: str | None = None,
        now: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO note_reuse (file_id, channel, status, note, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(file_id, channel) DO UPDATE SET
                    status = excluded.status,
                    note = COALESCE(excluded.note, note),
                    updated_at = excluded.updated_at
                """,
                (file_id, channel, status, note, now),
            )

    def clear_reuse(self, file_id: str, channel: str | None = None) -> int:
        with self._connect() as conn:
            if channel:
                cur = conn.execute(
                    "DELETE FROM note_reuse WHERE file_id = ? AND channel = ?",
                    (file_id, channel),
                )
            else:
                cur = conn.execute("DELETE FROM note_reuse WHERE file_id = ?", (file_id,))
            return cur.rowcount

    def list_reuse(self, file_id: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM note_reuse WHERE file_id = ? ORDER BY channel",
                    (file_id,),
                )
            )

    def query_reuse(
        self, *, channel: str | None = None, status: str | None = None
    ) -> list[sqlite3.Row]:
        """All reuse marks (joined with file title) for 'what material do I
        have for channel X' queries."""
        sql = """
            SELECT r.*, f.filename, f.start_time, f.edit_time
              FROM note_reuse r
              JOIN files f ON f.id = r.file_id
             WHERE f.is_trash = 0
        """
        params: list[str] = []
        if channel:
            sql += " AND r.channel = ?"
            params.append(channel)
        if status:
            sql += " AND r.status = ?"
            params.append(status)
        sql += " ORDER BY r.updated_at DESC"
        with self._connect() as conn:
            return list(conn.execute(sql, params))

    # ---------- dual-transcribe pipeline state ----------

    def get_dual(self, file_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM dual_pipeline WHERE file_id = ?", (file_id,)
            ).fetchone()

    def upsert_dual(
        self,
        file_id: str,
        *,
        now: int,
        status: str | None = None,
        speaker_proposal: str | None = None,
        speaker_map: str | None = None,
        vault_path: str | None = None,
        error: str | None = None,
    ) -> None:
        """None = leave unchanged — except `error`, which is always set to the
        given value so every state transition clears a stale error."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO dual_pipeline
                    (file_id, status, speaker_proposal, speaker_map, vault_path,
                     error, updated_at)
                VALUES (?, COALESCE(?, 'marked'), ?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    status = COALESCE(excluded.status, status),
                    speaker_proposal = COALESCE(excluded.speaker_proposal, speaker_proposal),
                    speaker_map = COALESCE(excluded.speaker_map, speaker_map),
                    vault_path = COALESCE(excluded.vault_path, vault_path),
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (
                    file_id,
                    status,
                    speaker_proposal,
                    speaker_map,
                    vault_path,
                    error,
                    now,
                ),
            )

    def delete_dual(self, file_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM dual_pipeline WHERE file_id = ?", (file_id,))

    def list_dual(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(conn.execute("SELECT * FROM dual_pipeline ORDER BY updated_at DESC"))

    def get_note_metadata(self, file_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM note_metadata WHERE file_id = ?", (file_id,)
            ).fetchone()

    def tag_counts(self, *, include_trash: bool = False) -> list[tuple[str, int]]:
        """All tags with how many (non-trash) files carry each, busiest first."""
        trash = "" if include_trash else "JOIN files f ON f.id = nt.file_id AND f.is_trash = 0"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT nt.tag AS tag, COUNT(DISTINCT nt.file_id) AS n
                  FROM note_tags nt
                  {trash}
                 GROUP BY nt.tag
                 ORDER BY n DESC, nt.tag COLLATE NOCASE
                """
            ).fetchall()
        return [(r["tag"], int(r["n"])) for r in rows]

    def file_ids_with_tag(self, tag: str) -> set[str]:
        with self._connect() as conn:
            return {
                r[0] for r in conn.execute("SELECT file_id FROM note_tags WHERE tag = ?", (tag,))
            }

    def list_note_tags(self, file_id: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT tag, source, created_at
                      FROM note_tags
                     WHERE file_id = ?
                     ORDER BY CASE source
                                WHEN 'manual' THEN 0
                                WHEN 'ai' THEN 1
                                WHEN 'auto' THEN 2
                                ELSE 3
                              END,
                              tag COLLATE NOCASE
                    """,
                    (file_id,),
                )
            )

    def add_note_tags(
        self,
        file_id: str,
        raw_tags: list[str],
        *,
        source: str = "manual",
        now: int,
    ) -> list[str]:
        tags = normalize_tags(raw_tags)
        if not tags:
            return []
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO note_tags (file_id, tag, source, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(file_id, tag) DO NOTHING
                """,
                [(file_id, tag, source, now) for tag in tags],
            )
        return tags

    def replace_generated_note_tags(
        self,
        file_id: str,
        raw_tags: list[str],
        *,
        source: str,
        now: int,
    ) -> list[str]:
        tags = normalize_tags(raw_tags)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM note_tags WHERE file_id = ? AND source IN ('ai', 'auto')",
                (file_id,),
            )
            if tags:
                conn.executemany(
                    """
                    INSERT INTO note_tags (file_id, tag, source, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(file_id, tag) DO NOTHING
                    """,
                    [(file_id, tag, source, now) for tag in tags],
                )
        return tags

    def remove_note_tags(self, file_id: str, raw_tags: list[str]) -> list[str]:
        tags = normalize_tags(raw_tags)
        if not tags:
            return []
        with self._connect() as conn:
            conn.executemany(
                "DELETE FROM note_tags WHERE file_id = ? AND tag = ?",
                [(file_id, tag) for tag in tags],
            )
        return tags

    def upsert_note_reference(
        self,
        *,
        file_id: str,
        path: Path | str,
        kind: str,
        title: str | None = None,
        now: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO note_references (file_id, path, kind, title, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(file_id, path) DO UPDATE SET
                    kind = excluded.kind,
                    title = COALESCE(excluded.title, title),
                    updated_at = excluded.updated_at
                """,
                (file_id, str(path), kind, title, now),
            )

    def list_note_references(self, file_id: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT path, kind, title, updated_at
                      FROM note_references
                     WHERE file_id = ?
                     ORDER BY updated_at DESC
                    """,
                    (file_id,),
                )
            )

    # ---------- speakers (saved list) ----------

    def add_speaker(self, *, name: str, is_self: bool = False, now: int) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO speakers (name, is_self, created_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET is_self = excluded.is_self
                   RETURNING id""",
                (name, 1 if is_self else 0, now),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def list_speakers(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT id, name, is_self, notes FROM speakers "
                    "ORDER BY is_self DESC, name COLLATE NOCASE"
                )
            )

    def delete_speaker(self, speaker_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM speakers WHERE id = ?", (speaker_id,))

    # ---------- cmds transcripts ----------

    def get_cmds_transcript(self, file_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                """SELECT * FROM cmds_transcripts WHERE file_id = ?
                    ORDER BY fetched_at DESC LIMIT 1""",
                (file_id,),
            ).fetchone()

    def update_cmds_segments(
        self, file_id: str, model: str, segments_json: str, *, now: int
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE cmds_transcripts
                      SET segments = ?, fetched_at = ?
                    WHERE file_id = ? AND model = ?""",
                (segments_json, now, file_id, model),
            )

    def save_cmds_transcript(
        self,
        *,
        file_id: str,
        model: str,
        language: str | None,
        text: str,
        segments_json: str,
        now: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cmds_transcripts
                    (file_id, model, language, text, segments, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id, model) DO UPDATE SET
                    language = excluded.language,
                    text = excluded.text,
                    segments = excluded.segments,
                    fetched_at = excluded.fetched_at
                """,
                (file_id, model, language, text, segments_json, now),
            )

    def files_missing_folder_link(self) -> list[sqlite3.Row]:
        """Files whose folder assignment has never been recorded.

        We check based on `file_content` cache absence as a proxy — once content
        is fetched, folder ids are saved alongside it. For untouched files we
        still want to reflect folder membership in the sidebar, so this lets
        the basic `sync` opportunistically pull folder ids for un-cached files.
        """
        with self._connect() as conn:
            return list(
                conn.execute("""
                SELECT id FROM files
                 WHERE is_trash = 0
                   AND id NOT IN (SELECT file_id FROM file_folders)
                   AND id NOT IN (SELECT file_id FROM file_content)
                 ORDER BY edit_time DESC
                 LIMIT 30
            """)
            )
