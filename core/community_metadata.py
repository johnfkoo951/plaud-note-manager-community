"""Community-safe note metadata generation over a user-supplied provider key.

This is the community counterpart to the private edition's vault-aware
generator.  It deliberately produces only *local* metadata — title,
description, note type, and tags.  There is no bundled taxonomy, no CMDS
category, and no vault destination: those belong to a personal vault layout
that a shared build must not assume.

Every external call goes through ``community_models.run_model``, which refuses
to run without an explicit per-run ``confirmed_external=True``.  This module
never lowers that bar on the caller's behalf.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import community_models
from .storage import Storage
from .tags import normalize_tags

MAX_SOURCE_CHARS = 16000

_PROMPT = """You are labelling one voice recording for a personal archive.

Return ONLY a JSON object, no prose and no code fence, with these keys:
  "title":       a short factual title, at most 80 characters
  "description": one or two sentences describing what the recording covers
  "note_type":   one of "meeting", "interview", "lecture", "note", "call", "other"
  "tags":        3 to 8 short lowercase topic tags, as a JSON array of strings

Write "title" and "description" in the same language as the source text.
Do not invent participants, dates, decisions, or numbers that are not present.

Source text:
---
{source}
---"""


class MetadataGenerationError(RuntimeError):
    """The provider replied, but not with metadata this build can use."""


def _source_text(row: Any) -> str:
    """Prefer the transcript; fall back to Plaud's own summary."""
    for key in ("transcript", "summary_md"):
        try:
            value = row[key]
        except (KeyError, IndexError, TypeError):
            value = None
        if value and str(value).strip():
            return str(value).strip()[:MAX_SOURCE_CHARS]
    return ""


def _extract_json(reply: str) -> dict[str, Any]:
    """Pull the JSON object out of a model reply that may be fenced or padded."""
    text = reply.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise MetadataGenerationError("provider reply contained no JSON object")
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise MetadataGenerationError(f"provider reply was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise MetadataGenerationError("provider reply was not a JSON object")
    return parsed


_NOTE_TYPES = frozenset({"meeting", "interview", "lecture", "note", "call", "other"})


def _clean(parsed: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields this edition owns, coerced to safe shapes."""
    out: dict[str, Any] = {}

    title = parsed.get("title")
    if isinstance(title, str) and title.strip():
        out["title"] = title.strip()[:200]

    description = parsed.get("description")
    if isinstance(description, str) and description.strip():
        out["description"] = description.strip()[:2000]

    note_type = parsed.get("note_type")
    if isinstance(note_type, str) and note_type.strip().lower() in _NOTE_TYPES:
        out["note_type"] = note_type.strip().lower()

    raw_tags = parsed.get("tags")
    if isinstance(raw_tags, str):
        raw_tags = [part for part in re.split(r"[,\n]", raw_tags)]
    if isinstance(raw_tags, list):
        tags = normalize_tags([t for t in raw_tags if isinstance(t, str)])
        if tags:
            out["tags"] = tags

    return out


def generate_note_metadata(
    storage: Storage,
    file_id: str,
    *,
    provider: str,
    backend: str,
    confirmed_external: bool = False,
    model_id: str | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    """Generate local metadata for one file. Returns the merged metadata dict.

    Raises ``MetadataGenerationError`` when the source is empty or the reply is
    unusable, and propagates ``community_models`` errors (including
    ``ExternalConsentRequired``) untouched.
    """
    rows = storage.files_for_auto_metadata(fetched_after=None)
    row = next((r for r in rows if r["file_id"] == file_id), None)
    if row is None:
        raise MetadataGenerationError(f"{file_id} has no cached transcript or summary")

    source = _source_text(row)
    if not source:
        raise MetadataGenerationError(f"{file_id} has no usable source text")

    reply = community_models.run_model(
        provider,
        backend,
        _PROMPT.format(source=source),
        confirmed_external=confirmed_external,
        model_id=model_id,
        timeout=timeout,
    )
    fields = _clean(_extract_json(reply))
    if not fields:
        raise MetadataGenerationError("provider reply held no usable metadata fields")

    existing: dict[str, Any] = {}
    raw = row["metadata_json"] if "metadata_json" in row.keys() else None
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                existing = parsed
        except json.JSONDecodeError:
            existing = {}

    merged = {**existing, **fields}
    merged["file_id"] = file_id
    merged["usage_status"] = merged.get("usage_status") or "metadata-ready"
    return merged
