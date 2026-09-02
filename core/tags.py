"""Obsidian-style tag normalization shared by CLI, storage, and app helpers."""

from __future__ import annotations

import re
import unicodedata

_SPACE_RE = re.compile(r"\s+")
_DASH_RE = re.compile(r"-+")
_DISALLOWED_RE = re.compile(r"[\[\]{}()<>#`'\"|\\/:;,.!?]+")


def normalize_tag(raw: str) -> str:
    """Return a plain Obsidian frontmatter tag: no hash, no spaces.

    This collapses ``, ， ;`` to ``-`` within a single tag. Treating those as
    tag *separators* is the job of :func:`normalize_tags`, which splits first;
    callers that want "a, b" to become two tags must use that function.
    """
    tag = unicodedata.normalize("NFKC", raw or "").strip()
    tag = tag.lstrip("#").strip()
    tag = _SPACE_RE.sub("-", tag)
    tag = _DISALLOWED_RE.sub("-", tag)
    tag = _DASH_RE.sub("-", tag).strip("-_")
    return tag


def normalize_tags(raw_tags: list[str]) -> list[str]:
    """Normalize, deduplicate, and preserve first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_tags:
        for part in re.split(r"[,，;]+", raw or ""):
            tag = normalize_tag(part)
            if not tag or tag in seen:
                continue
            seen.add(tag)
            out.append(tag)
    return out
