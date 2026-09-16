"""Bounded reads through the separately installed official ``@plaud-ai/cli``.

The project's own ``plaud`` executable is a different program. Never resolve it
through PATH, copy its credentials, or start OAuth from this adapter. Discovery
reads package metadata; only an explicit live check/read invokes the official
CLI, which manages its own OAuth refresh. No Cloud recording writes are exposed.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PACKAGE_NAME = "@plaud-ai/cli"
AUDITED_VERSION = "0.3.11"
BLOCKS = ("transaction", "transaction_polish", "outline", "mark_memo")
CAPABILITIES = ("current_user", "summary_notes_json", *BLOCKS)
_FILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_OFFICIAL_ENV = {
    "PLAUD_CLI_CLIENT_ID",
    "PLAUD_CLIENT_ID",
    "PLAUD_CLIENT_SECRET",
    "PLAUD_API_BASE",
    "PLAUD_AUTH_URL",
    "PLAUD_TOKEN_URL",
    "PLAUD_REFRESH_URL",
    "PLAUD_ENV",
    "PLAUD_REGION",
}


class OfficialCLIError(RuntimeError):
    """A safe diagnostic; never includes subprocess output or credentials."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Installation:
    package_path: Path
    entrypoint: Path
    node_path: Path
    version: str


def _node_version(path: Path) -> tuple[int, ...]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", path.name)
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def _candidate_packages() -> list[tuple[Path, Path]]:
    explicit = os.environ.get("PLAUD_OFFICIAL_CLI_PACKAGE")
    if explicit:
        package = Path(explicit).expanduser()
        if not package.is_absolute():
            raise OfficialCLIError("invalid_installation", "The package override must be absolute.")
        default_node = package.parents[3] / "bin/node" if len(package.parents) >= 4 else None
        node = os.environ.get("PLAUD_OFFICIAL_NODE")
        if not node and default_node is None:
            raise OfficialCLIError("invalid_installation", "An absolute Node path is required.")
        return [(package, Path(node).expanduser() if node else default_node)]

    home = Path.home()
    nvm = home / ".nvm/versions/node"
    versions = sorted(nvm.glob("v*"), key=lambda p: (_node_version(p), p.name), reverse=True)
    prefixes = [*versions, home / ".local", Path("/opt/homebrew"), Path("/usr/local")]
    return [(prefix / "lib/node_modules/@plaud-ai/cli", prefix / "bin/node") for prefix in prefixes]


def _validate_installation(package: Path, node: Path) -> Installation | None:
    try:
        if not node.is_absolute() or not node.is_file() or not os.access(node, os.X_OK):
            return None
        package = package.resolve()
        metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or metadata.get("name") != PACKAGE_NAME:
            return None
        version = metadata.get("version")
        entry = metadata.get("bin", {})
        if isinstance(entry, dict):
            entry = entry.get("plaud")
        if not isinstance(version, str) or not re.fullmatch(
            r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", version
        ):
            return None
        if not isinstance(entry, str):
            return None
        entrypoint = (package / entry).resolve()
        if not entrypoint.is_relative_to(package) or not entrypoint.is_file():
            return None
        return Installation(package, entrypoint, node.resolve(), version)
    except (OSError, ValueError, TypeError):
        return None


def discover() -> Installation | None:
    """Locate official package metadata and its sibling Node without PATH lookup.

    ``PLAUD_OFFICIAL_CLI_PACKAGE`` can select an absolute package directory;
    ``PLAUD_OFFICIAL_NODE`` can accompany it for a nonstandard installation.
    An invalid explicit selection fails closed instead of selecting another copy.
    Metadata validation establishes package identity, not a cryptographic audit.
    """
    explicit = bool(os.environ.get("PLAUD_OFFICIAL_CLI_PACKAGE"))
    for package, node in _candidate_packages():
        installation = _validate_installation(package, node)
        if installation:
            return installation
    if explicit:
        raise OfficialCLIError(
            "invalid_installation", "The selected official CLI installation is invalid."
        )
    return None


def _run(installation: Installation, args: list[str], timeout: float) -> str:
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 120:
        raise OfficialCLIError(
            "invalid_timeout", "Timeout must be greater than 0 and at most 120 seconds."
        )
    env = dict(os.environ)
    # The upstream package imports dotenv/config. Never load our project's .env
    # (private web credentials), and do not enable ambient Node code injection.
    for key in list(env):
        if (
            key.startswith("DOTENV_CONFIG_")
            or key == "NODE_OPTIONS"
            or (key.startswith("PLAUD_") and key not in _OFFICIAL_ENV)
        ):
            env.pop(key)
    env.update(
        DOTENV_CONFIG_PATH=os.devnull,
        NO_COLOR="1",
        FORCE_COLOR="0",
        DO_NOT_TRACK="1",
        PLAUD_TELEMETRY_DISABLED="1",
    )
    try:
        result = subprocess.run(
            [str(installation.node_path), str(installation.entrypoint), *args],
            cwd=installation.package_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise OfficialCLIError("timeout", "The official CLI request timed out.") from None
    except OSError:
        raise OfficialCLIError("unavailable", "The official CLI could not be started.") from None
    if result.returncode:
        code, detail = {
            2: ("needs_login", "The official CLI requires its own Plaud login."),
            3: ("network_error", "The official CLI could not reach Plaud."),
            4: ("timeout", "The official CLI request timed out."),
        }.get(result.returncode, ("command_failed", "The official CLI request failed."))
        raise OfficialCLIError(code, detail)
    return result.stdout


def status(*, live: bool = False, timeout: float = 15) -> dict:
    """Return safe installation/auth diagnostics, without reading token contents.

    ``live=True`` requests ``me``; upstream may refresh its own OAuth token, but
    account output is discarded. A token file alone never proves valid auth.
    """
    try:
        token_file_exists = stat.S_ISREG((Path.home() / ".plaud/tokens.json").stat().st_mode)
    except OSError:
        token_file_exists = False
    result = {
        "provider": "official-cli",
        "installed": False,
        "version": None,
        "package_path": None,
        "node_path": None,
        "audited_version": AUDITED_VERSION,
        "capabilities": [],
        "token_file_exists": token_file_exists,
        "auth_state": "not_checked",
        "live_checked": False,
        "detail": "The official CLI is not installed in a supported location.",
    }
    try:
        installation = discover()
        if not installation:
            return result
        result.update(
            installed=True,
            version=installation.version,
            package_path=str(installation.package_path),
            node_path=str(installation.node_path),
            capabilities=list(CAPABILITIES),
            detail="Installed; token-file presence does not verify authentication.",
        )
        if live:
            result["live_checked"] = True
            _run(installation, ["me"], timeout)
            result.update(auth_state="valid", detail="The official CLI authenticated successfully.")
    except OfficialCLIError as exc:
        result.update(auth_state=exc.code, detail=exc.detail)
    return result


def _note_text(note: dict, key: str) -> str:
    value = note.get(key)
    return value if isinstance(value, str) else ""


def read_recording(
    file_id: str,
    kind: Literal["summary", "transcript"] = "summary",
    *,
    block: str = "transaction",
    timeout: float = 30,
) -> dict:
    """Read a known recording ID without mutating our cache or the Cloud.

    Summary preserves every note's text/tab/title/type from upstream JSON.
    Link-only notes are explicitly incomplete: their presigned URLs are omitted
    and must be materialized with official MCP ``get_note`` before cache ingest.
    Transcript is the CLI's rendered export, not canonical segment JSON. Missing
    output (even CLI exit 0) is unavailable and must not mark content cached.
    """
    if not isinstance(file_id, str) or not _FILE_ID.fullmatch(file_id):
        raise OfficialCLIError("invalid_file_id", "A valid recording ID is required.")
    if kind not in ("summary", "transcript"):
        raise OfficialCLIError("invalid_kind", "Only summary and transcript reads are supported.")
    if block not in BLOCKS:
        raise OfficialCLIError("invalid_block", "The requested transcript block is unsupported.")
    installation = discover()
    if not installation:
        raise OfficialCLIError(
            "not_installed", "The official CLI is not installed in a supported location."
        )
    result = {
        "provider": "official-cli",
        "package_version": installation.version,
        "file_id": file_id,
        "kind": kind,
        "block": block if kind == "transcript" else None,
        "available": False,
        "complete": False,
        "text": "",
        "notes": [],
        "detail": "No content is available for the requested recording block.",
    }
    if kind == "summary":
        raw = _run(installation, ["summary", file_id, "--json"], timeout)
        try:
            notes = json.loads(raw)
        except ValueError:
            raise OfficialCLIError(
                "invalid_output", "The official CLI returned invalid note JSON."
            ) from None
        if not isinstance(notes, list) or not all(isinstance(note, dict) for note in notes):
            raise OfficialCLIError(
                "invalid_output", "The official CLI returned an unexpected note schema."
            )
        result["notes"] = [
            {
                "type": _note_text(note, "data_type"),
                "title": _note_text(note, "data_title"),
                "tab_name": _note_text(note, "data_tab_name"),
                "content": _note_text(note, "data_content"),
                "linked": bool(note.get("data_link")),
            }
            for note in notes
        ]
        result["text"] = "\n\n".join(note["content"] for note in result["notes"] if note["content"])
        result["available"] = bool(result["text"].strip())
        result["complete"] = bool(notes) and all(
            note["content"].strip() for note in result["notes"]
        )
        if result["complete"]:
            result["detail"] = "All returned note bodies are present; tab metadata is preserved."
        elif notes:
            result["detail"] = (
                "Some note bodies are absent; use official MCP get_note for linked content."
            )
        return result

    with tempfile.TemporaryDirectory(prefix="plaud-official-read-") as temp_dir:
        output = Path(temp_dir) / "transcript.txt"
        _run(
            installation,
            ["transcript", file_id, "--block", block, "--output", str(output)],
            timeout,
        )
        if output.is_file():
            try:
                result["text"] = output.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise OfficialCLIError(
                    "invalid_output", "The transcript export could not be read."
                ) from None
            result["available"] = result["complete"] = bool(result["text"].strip())
            if result["complete"]:
                result["detail"] = (
                    "Read the official CLI text export; structured segments are not inferred."
                )
    return result
