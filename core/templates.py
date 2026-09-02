"""Load and manage prompt templates from `templates/`.

A template is a markdown file with optional YAML-ish frontmatter:

    ---
    name: meeting
    description: Meeting minutes summary
    ---

    <prompt body with {placeholders}>

Supported placeholders: {title}, {keywords}, {speakers}, {transcript}.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .paths import TEMPLATES_DIR, template_path


@dataclass
class Template:
    name: str
    description: str
    body: str
    path: Path

    def render(
        self,
        *,
        transcript: str = "",
        title: str = "",
        keywords: str = "",
        speakers: str = "",
        cmds_transcript: str = "",
        plaud_transcript: str = "",
        plaud_summaries: str = "",
        **extra: str,
    ) -> str:
        values = {
            "transcript": transcript,
            "title": title,
            "keywords": keywords,
            "speakers": speakers,
            "cmds_transcript": cmds_transcript,
            "plaud_transcript": plaud_transcript,
            "plaud_summaries": plaud_summaries,
            **{key: str(value) for key, value in extra.items()},
        }
        out = self.body
        for key, value in values.items():
            out = out.replace("{" + key + "}", value)
        return out


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def load_template(name: str) -> Template:
    path = template_path(name)
    if not path.exists():
        raise FileNotFoundError(f"template not found: {name} ({path})")
    raw = path.read_text(encoding="utf-8")
    description = ""
    body = raw
    if m := _FRONTMATTER_RE.match(raw):
        meta = m.group(1)
        for line in meta.splitlines():
            if line.startswith("description:"):
                description = line.split(":", 1)[1].strip()
        body = raw[m.end() :]
    return Template(name=name, description=description, body=body, path=path)


def list_templates() -> list[Template]:
    if not TEMPLATES_DIR.exists():
        return []
    out: list[Template] = []
    for p in sorted(TEMPLATES_DIR.glob("*.md")):
        try:
            out.append(load_template(p.stem))
        except Exception:
            continue
    return out


def save_template(name: str, body: str, *, description: str = "") -> Path:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    path = template_path(name)
    front = f"---\nname: {name}\ndescription: {description}\n---\n\n"
    path.write_text(front + body, encoding="utf-8")
    return path


def delete_template(name: str) -> None:
    path = template_path(name)
    if path.exists():
        path.unlink()
