"""Derived per-file pipeline progress: new → cached → transcribed → integrated.

`files.status` stayed 'new' for almost every row because nothing advanced it.
Progress is instead DERIVED from the artifacts that actually exist — a
file_content row (Plaud detail cache), a cmds_transcripts row (CMDS STT), and
integrated outputs on disk — so it can never go stale. The agent loop and the
app's Progress tile share this definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .paths import integrated_base
from .storage import Storage

# Ordered low → high; a file's stage is the highest one reached.
STAGES = ("new", "cached", "transcribed", "integrated")


@dataclass(frozen=True)
class Progress:
    counts: dict[str, int]  # stage -> file count (every stage key present)
    stages: dict[str, str]  # file_id -> stage


def integrated_file_ids(base: Path | None = None) -> set[str]:
    """File ids that have at least one integrated .md output on disk."""
    base = base or integrated_base()
    if not base.is_dir():
        return set()
    return {d.name for d in base.iterdir() if d.is_dir() and any(d.glob("*.md"))}


def derive_progress(
    storage: Storage | None = None, *, integrated_root: Path | None = None
) -> Progress:
    storage = storage or Storage()
    files = storage.active_file_ids()
    cached = storage.cached_file_ids() & files
    transcribed = storage.cmds_transcribed_file_ids() & files
    integrated = integrated_file_ids(integrated_root) & files

    stages: dict[str, str] = {}
    for fid in files:
        if fid in integrated:
            stages[fid] = "integrated"
        elif fid in transcribed:
            stages[fid] = "transcribed"
        elif fid in cached:
            stages[fid] = "cached"
        else:
            stages[fid] = "new"

    counts = dict.fromkeys(STAGES, 0)
    for stage in stages.values():
        counts[stage] += 1
    return Progress(counts=counts, stages=stages)
