"""Domain models shared across CLI / agent / app."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class FileStatus(str, Enum):
    NEW = "new"
    DOWNLOADED = "downloaded"
    TRANSCRIBED = "transcribed"
    PROCESSED = "processed"
    ARCHIVED = "archived"


class PlaudFile(BaseModel):
    """Subset of Plaud Cloud file metadata we care about."""

    id: str
    filename: str | None = None
    fullname: str | None = None
    filesize: int | None = None
    duration: float | None = None
    edit_from: str | None = None
    edit_time: int | None = None
    start_time: int | None = None
    end_time: int | None = None


class FileListPage(BaseModel):
    total: int = 0
    items: list[PlaudFile] = Field(default_factory=list)


class Folder(BaseModel):
    id: str
    name: str
    icon: str | None = None
    color: str | None = None


class TranscriptSegment(BaseModel):
    start_time: int
    end_time: int
    content: str
    speaker: str | None = None


class OutlineItem(BaseModel):
    start_time: int
    end_time: int
    topic: str


class SummaryBlock(BaseModel):
    """One summary block — auto_sum_note or one of the template-based sum_multi_note."""

    kind: str  # "auto_sum_note" | "sum_multi_note" | other
    title: str | None = None
    body_md: str = ""


class FileContent(BaseModel):
    """Cached content blocks for a single file."""

    file_id: str
    title: str | None = None
    transcript: list[TranscriptSegment] = Field(default_factory=list)
    outline: list[OutlineItem] = Field(default_factory=list)
    summaries: list[SummaryBlock] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    folder_ids: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when Plaud returned nothing usable — typically because the
        recording is still being processed. Such results must NOT be cached as
        final, or the file gets stuck showing 'no content' forever."""
        return not self.transcript and not self.summaries and not self.outline

    @property
    def summary_md(self) -> str | None:
        for s in self.summaries:
            if s.kind == "auto_sum_note":
                return s.body_md
        return None

    @property
    def summary_extra_md(self) -> list[str]:
        return [s.body_md for s in self.summaries if s.kind != "auto_sum_note"]

    def transcript_text(self) -> str:
        return "\n".join(
            f"[{_fmt_ts(s.start_time)}] {s.speaker or ''}: {s.content}".strip()
            for s in self.transcript
        )

    def outline_text(self) -> str:
        return "\n".join(f"- [{_fmt_ts(o.start_time)}] {o.topic}" for o in self.outline)


def _fmt_ts(ms: int) -> str:
    s = max(0, ms // 1000)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
