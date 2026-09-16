"""Automatic metadata generation for freshly synced recordings.

Community edition: generation is routed through ``community_metadata``
over a user-supplied provider key, and every batch needs an explicit
``confirmed_external=True``. Only local metadata is written — this module
never touches Plaud Cloud and never assumes a vault.

Runs after `sync-content` (CLI, app Backfill, and the agent loop all route
through it). Policy, per PLAN-v0.6:

- Only files with a cached transcript or summary are eligible.
- Default scope is *recent* files (content fetched within `since_days`);
  the historical backlog is opt-in via backfill=True.
- Folder placement stays suggestion-only here — `generate_note_metadata`
  fills `folder_name`/confidence but nothing is moved.
- Idempotent: a sha256 over the source text is stored in
  metadata_json["auto_meta"]; unchanged sources are skipped.
- Failures back off exponentially (1h · 2^(attempts-1), capped at 24h) so a
  broken provider can't stall every sync.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from . import app_config
from .storage import Storage

BACKOFF_BASE_S = 3600
BACKOFF_CAP_S = 24 * 3600
DEFAULT_SINCE_DAYS = 7


@dataclass
class AutoMetadataReport:
    model: str = ""
    generated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    backed_off: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    remaining: int = 0  # eligible but over the batch limit
    aborted: str = ""  # non-empty = nothing ran (e.g. model CLI missing)

    def summary(self) -> str:
        if self.aborted:
            return f"auto-metadata aborted: {self.aborted}"
        parts = [f"generated {len(self.generated)}"]
        if self.unchanged:
            parts.append(f"unchanged {len(self.unchanged)}")
        if self.backed_off:
            parts.append(f"backed-off {len(self.backed_off)}")
        if self.failed:
            parts.append(f"failed {len(self.failed)}")
        if self.remaining:
            parts.append(f"remaining {self.remaining}")
        return f"auto-metadata ({self.model}): " + ", ".join(parts)


def source_hash(row: Any) -> str:
    """Stable digest of the Plaud-side source text metadata derives from."""
    payload = "\x1f".join(
        str(row[key] or "") for key in ("transcript", "summary_md", "summary_extra")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _auto_meta(metadata_json: str | None) -> dict[str, Any]:
    if not metadata_json:
        return {}
    try:
        parsed = json.loads(metadata_json)
    except Exception:
        return {}
    info = parsed.get("auto_meta")
    return info if isinstance(info, dict) else {}


def _in_backoff(info: dict[str, Any], now: int) -> bool:
    attempts = int(info.get("attempts") or 0)
    last = int(info.get("last_attempt") or 0)
    if attempts <= 0 or not last:
        return False
    wait = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (attempts - 1)))
    return now - last < wait


def run_auto_metadata(
    storage: Storage,
    *,
    provider: str,
    backend: str,
    confirmed_external: bool = False,
    model_id: str | None = None,
    limit: int | None = None,
    since_days: int = DEFAULT_SINCE_DAYS,
    backfill: bool = False,
    dry_run: bool = False,
) -> AutoMetadataReport:
    from .community_metadata import generate_note_metadata
    from .community_models import model_available

    limit = limit if limit is not None else app_config.auto_metadata_limit()
    report = AutoMetadataReport(model=f"{provider}/{backend}")

    if not dry_run and not confirmed_external:
        report.aborted = "external model use requires an explicit confirmation"
        return report

    if not dry_run and not model_available(provider, backend):
        # Without a reachable provider generate_note_metadata would silently
        # mass-produce keyword-fallback metadata — worse than doing nothing.
        report.aborted = (
            f"model '{provider}/{backend}' unavailable (CLI missing and no API key)"
        )
        return report

    now = int(time.time())
    cutoff = None if backfill else now - since_days * 86400
    candidates = storage.files_for_auto_metadata(fetched_after=cutoff)

    todo: list[Any] = []
    for row in candidates:
        info = _auto_meta(row["metadata_json"])
        digest = source_hash(row)
        if info.get("source_hash") == digest:
            # Source unchanged since the last successful run. Bump
            # generated_at so the SQL prefilter stops reselecting this file.
            report.unchanged.append(row["file_id"])
            if not dry_run:
                storage.upsert_note_metadata(
                    file_id=row["file_id"],
                    generated_at=max(now, int(row["fetched_at"] or 0)),
                    now=now,
                )
            continue
        if _in_backoff(info, now):
            report.backed_off.append(row["file_id"])
            continue
        todo.append(row)

    report.remaining = max(0, len(todo) - limit)
    consecutive_failures = 0
    attempted = 0
    for index, row in enumerate(todo[:limit]):
        file_id = row["file_id"]
        if dry_run:
            report.generated.append(file_id)
            continue
        if should_abort_batch(consecutive_failures, len(report.generated), attempted):
            report.aborted = (
                f"stopped after {consecutive_failures} consecutive failures; "
                f"{len(todo[:limit]) - index} file(s) left untried"
            )
            report.remaining += len(todo[:limit]) - index
            break
        attempted += 1
        try:
            merged = generate_note_metadata(
                storage,
                file_id,
                provider=provider,
                backend=backend,
                confirmed_external=confirmed_external,
                model_id=model_id,
            )
        except Exception as exc:  # provider timeout, JSON parse, etc.
            report.failed[file_id] = str(exc)
            consecutive_failures += 1
            _record_attempt(storage, row, now=int(time.time()))
            continue
        merged["auto_meta"] = {
            "source_hash": source_hash(row),
            "at": int(time.time()),
            "attempts": 0,
        }
        storage.upsert_note_metadata(file_id=file_id, metadata=merged, now=int(time.time()))
        consecutive_failures = 0
        report.generated.append(file_id)
    return report


def should_abort_batch(
    consecutive_failures: int,
    generated: int,
    attempted: int,
) -> bool:
    """Decide whether to stop a consented batch early.

    In this edition every generated file is one billable call against the
    user's own provider key, and every failure also writes a backoff marker.
    A revoked key, an exhausted quota, or a provider outage looks the same
    from here: calls that keep failing. Without a stop rule a single bad
    config burns the whole batch.

    Args:
        consecutive_failures: failures since the last success (resets on success).
        generated: files successfully written so far this run.
        attempted: files tried so far this run, successes and failures.

    Returns:
        True to stop the batch now and report what was done.
    """
    # Three failures in a row is the signature of a broken configuration —
    # a revoked key, an exhausted quota, a provider outage — rather than one
    # awkward recording. Stopping there keeps a bad run cheap while staying
    # tolerant of isolated failures, and the untried files stay eligible for
    # the next run instead of being marked and backed off.
    return consecutive_failures >= 3


def _record_attempt(storage: Storage, row: Any, *, now: int) -> None:
    """Persist a failed attempt without clobbering any existing metadata."""
    existing: dict[str, Any] = {}
    if row["metadata_json"]:
        try:
            parsed = json.loads(row["metadata_json"])
            if isinstance(parsed, dict):
                existing = parsed
        except Exception:
            pass
    info = existing.get("auto_meta")
    info = dict(info) if isinstance(info, dict) else {}
    info["attempts"] = int(info.get("attempts") or 0) + 1
    info["last_attempt"] = now
    existing["auto_meta"] = info
    storage.upsert_note_metadata(file_id=row["file_id"], metadata=existing, now=now)
