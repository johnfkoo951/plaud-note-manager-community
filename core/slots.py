"""User-configured summary slots (model + template combinations)."""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict

from .paths import SLOTS_FILE


@dataclass
class Slot:
    name: str
    model: str
    template: str
    model_id: str = ""


DEFAULT_SLOTS: list[Slot] = [
    Slot(name="Default (Claude)", model="claude", template="default", model_id=""),
]


def load_slots() -> list[Slot]:
    if not SLOTS_FILE.exists():
        return list(DEFAULT_SLOTS)
    try:
        data = json.loads(SLOTS_FILE.read_text(encoding="utf-8"))
        return [Slot(**d) for d in data]
    except Exception:
        return list(DEFAULT_SLOTS)


def save_slots(slots: list[Slot]) -> None:
    SLOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SLOTS_FILE.write_text(
        json.dumps([asdict(s) for s in slots], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
