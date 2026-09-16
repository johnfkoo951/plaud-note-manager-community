"""User-tunable application config: backend mode per model + output paths.

Lives at `data/config.json`. CLI commands `plaud config-*` and the SwiftUI
Settings sheet read/write the same file so changes are instantly visible to
both surfaces.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from copy import deepcopy
from pathlib import Path
from typing import Literal

from .paths import DATA_DIR
from .distribution import COMMUNITY_EDITION

CONFIG_FILE = DATA_DIR / "config.json"
_CONFIG_LOCK = threading.RLock()

Backend = Literal["cli", "api"]

DEFAULT_CONFIG: dict = {
    "backends": {
        "claude": "cli",
        "codex": "cli",
        "gemini": "api",
        "grok": "api",
    },
    "models": {
        # Used only when backend = api. CLI mode picks whatever the CLI
        # defaults to.
        "claude": "claude-fable-5",
        "codex": "gpt-5.6-sol",
        "gemini": "gemini-3.1-pro-preview",
        "grok": "grok-4.6",
    },
    "paths": {
        # Empty string = fall back to the project default.
        "transcripts": "",
        "summaries": "",
        "integrated": "",
    },
    # Personal / environment-specific locations. Empty = unset; a fresh clone
    # gets safe empty defaults and the user opts in via env or `plaud config-*`.
    "obsidian_vault": "",
    # Wiki (LLM satellite) vault for `vault-send --to wiki`. Empty = derive the
    # Optional sibling knowledge-base directory next to a configured vault.
    "wiki_vault": "",
    "author": "",
    "api_info_dir": "",
    # Which model runs auto-classify (its backend — cli subscription vs api
    # key — follows the per-model "backends" entry above).
    "classify_model": "claude",
    # Local deterministic routing always runs first. A provider can arbitrate
    # only decisions below this threshold and only after an explicit per-run
    # external-processing confirmation; this preference is not consent.
    "folder_llm_threshold": 0.8,
    # Which model runs metadata-generate. Default is codex (GPT via the Codex
    # CLI's ChatGPT subscription login — no API key needed while
    # backends.codex stays "cli"). Empty = fall back to classify_model.
    "metadata_model": "codex",
    # Auto-generate metadata for eligible files right after sync (files with a
    # cached transcript/summary and no metadata yet). Folder placement stays
    # suggestion-only in this path.
    "auto_metadata": False,
    # Max files processed per auto-metadata batch (cost/latency guard).
    "auto_metadata_limit": 20,
    # Tags the user pinned to the top of the app's Tags sidebar.
    "pinned_tags": [],
}


def load() -> dict:
    with _CONFIG_LOCK:
        if not CONFIG_FILE.exists():
            return deepcopy(DEFAULT_CONFIG)
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return deepcopy(DEFAULT_CONFIG)
        # Merge with defaults so new keys are filled in.
        merged = deepcopy(DEFAULT_CONFIG)
        for key, val in data.items():
            if isinstance(val, dict) and isinstance(merged.get(key), dict):
                merged[key].update(val)
            else:
                merged[key] = val
        return merged


def save(config: dict) -> None:
    with _CONFIG_LOCK:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(config, indent=2, ensure_ascii=False).encode("utf-8")
        nonce = secrets.token_hex(8)
        temporary = CONFIG_FILE.with_name(f".{CONFIG_FILE.name}.tmp-{os.getpid()}-{nonce}")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temporary, CONFIG_FILE)
        finally:
            if fd >= 0:
                os.close(fd)
            temporary.unlink(missing_ok=True)


def backend_for(model: str) -> Backend:
    return load().get("backends", {}).get(model, "cli")  # type: ignore[return-value]


def model_id_for(model: str) -> str:
    return load().get("models", {}).get(model, "")


def path_override(kind: str) -> Path | None:
    raw = load().get("paths", {}).get(kind, "")
    if not raw:
        return None
    return Path(os.path.expanduser(raw)).resolve()


def obsidian_vault() -> Path | None:
    """Resolved Obsidian vault path: env > config > None."""
    raw = os.environ.get("PLAUD_OBSIDIAN_VAULT") or load().get("obsidian_vault", "")
    if not raw:
        return None
    return Path(os.path.expanduser(raw)).resolve()


def author() -> str:
    """Author name for generated notes: env > config > empty string."""
    return os.environ.get("PLAUD_AUTHOR") or load().get("author", "") or ""


def api_info_dir() -> Path | None:
    """CMDS API Information directory: env > config > derived from vault > None."""
    raw = os.environ.get("PLAUD_API_INFO_DIR") or load().get("api_info_dir", "")
    if raw:
        return Path(os.path.expanduser(raw)).resolve()
    vault = obsidian_vault()
    if vault:
        return vault / "40. Docs/49. API Information"
    return None


def classify_model() -> str:
    """Model used by auto-classify when no --model given."""
    return load().get("classify_model") or "claude"


def set_classify_model(model: str) -> None:
    cfg = load()
    cfg["classify_model"] = model
    save(cfg)


def folder_llm_threshold() -> float:
    try:
        value = float(load().get("folder_llm_threshold", 0.8))
    except (TypeError, ValueError):
        return 0.8
    return min(1.0, max(0.0, value))


def set_folder_llm_threshold(value: float) -> None:
    if not 0.0 <= value <= 1.0:
        raise ValueError("folder LLM threshold must be between 0 and 1")
    cfg = load()
    cfg["folder_llm_threshold"] = float(value)
    save(cfg)


def metadata_model() -> str:
    """Model used by metadata-generate when no --model given.

    Falls back to classify_model when unset so older configs keep their
    previous behavior.
    """
    return load().get("metadata_model") or classify_model()


def set_metadata_model(model: str) -> None:
    cfg = load()
    cfg["metadata_model"] = model
    save(cfg)


def auto_metadata_enabled() -> bool:
    if COMMUNITY_EDITION:
        return False
    return bool(load().get("auto_metadata", False))


def set_auto_metadata(enabled: bool) -> None:
    cfg = load()
    cfg["auto_metadata"] = bool(enabled)
    save(cfg)


def auto_metadata_limit() -> int:
    try:
        limit = int(load().get("auto_metadata_limit", 20))
    except (TypeError, ValueError):
        return 20
    return max(1, limit)


def pinned_tags() -> list[str]:
    raw = load().get("pinned_tags") or []
    return [t for t in raw if isinstance(t, str)]


def set_pinned_tags(tags: list[str]) -> None:
    cfg = load()
    # De-dupe, preserve order.
    seen: set[str] = set()
    cleaned = [t for t in tags if t and not (t in seen or seen.add(t))]
    cfg["pinned_tags"] = cleaned
    save(cfg)


def toggle_pinned_tag(tag: str) -> bool:
    """Pin/unpin one tag. Returns True if now pinned."""
    tags = pinned_tags()
    if tag in tags:
        tags.remove(tag)
        set_pinned_tags(tags)
        return False
    tags.append(tag)
    set_pinned_tags(tags)
    return True


def set_backend(model: str, backend: Backend) -> None:
    cfg = load()
    cfg["backends"][model] = backend
    save(cfg)


def set_model_id(model: str, model_id: str) -> None:
    cfg = load()
    cfg["models"][model] = model_id
    save(cfg)


def set_routing_settings(provider: str, backend: Backend, model_id: str) -> None:
    """Commit the three routing choices as one in-process transaction."""
    with _CONFIG_LOCK:
        cfg = load()
        cfg["classify_model"] = provider
        cfg["backends"][provider] = backend
        cfg["models"][provider] = model_id
        save(cfg)


def set_path(kind: str, path: str) -> None:
    cfg = load()
    cfg["paths"][kind] = path
    save(cfg)


def set_obsidian_vault(path: str) -> None:
    cfg = load()
    cfg["obsidian_vault"] = path
    save(cfg)


def set_author(name: str) -> None:
    cfg = load()
    cfg["author"] = name
    save(cfg)
