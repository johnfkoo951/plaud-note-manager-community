#!/usr/bin/env python3
"""Static privacy and x64 architecture audit for the Windows portable build."""

from __future__ import annotations

import re
import stat
import struct
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

APP_NAME = "Plaud Note Manager Community"
TEXT_SUFFIXES = {".py", ".js", ".css", ".html", ".txt", ".md", ".cmd", "._pth"}
PE_SUFFIXES = {".exe", ".dll", ".pyd"}
SECRET_PATTERN = re.compile(
    rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|"
    rb"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|"
    rb"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|"
    rb"xox[baprs]-[0-9A-Za-z-]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,}|"
    rb"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"
)
PRIVATE_PATTERN = re.compile(
    rb"/Users/[A-Za-z0-9._-]+/|file:///Users/|yohankoo|johnfkoo951|Yohan's|"
    + "구요한".encode("utf-8"),
    flags=re.IGNORECASE,
)


class Audit:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, condition: bool, success: str, failure: str) -> None:
        if condition:
            print(f"[PASS] {success}")
        else:
            print(f"[FAIL] {failure}", file=sys.stderr)
            self.failures.append(failure)


def pe_machine(path: Path) -> int | None:
    data = path.read_bytes()
    if len(data) < 64 or data[:2] != b"MZ":
        return None
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if offset + 6 > len(data) or data[offset : offset + 4] != b"PE\0\0":
        return None
    return struct.unpack_from("<H", data, offset + 4)[0]


def audit_directory(root: Path) -> int:
    audit = Audit()
    files = [path for path in root.rglob("*") if path.is_file()]
    relative = {path.relative_to(root).as_posix() for path in files}
    required = {
        "Start Plaud Community.cmd",
        "Diagnose Plaud Community.cmd",
        "README-WINDOWS.txt",
        "BUILD-INFO.txt",
        "runtime/python.exe",
        "runtime/pythonw.exe",
        "runtime/python313.dll",
        "runtime/python313.zip",
        "runtime/python313._pth",
        "app/core/secret_store.py",
        "app/windows_app/__main__.py",
        "app/windows_app/launcher.py",
        "app/windows_app/server.py",
        "app/windows_app/static/index.html",
        "app/LICENSE",
        "app/PYTHON_LICENSE.txt",
        "app/THIRD_PARTY_NOTICES.md",
    }
    audit.check(
        required <= relative,
        "Required Windows runtime files are present.",
        "Required Windows runtime files are missing.",
    )

    template_names = {
        path.name for path in (root / "app" / "templates").glob("*.md") if path.is_file()
    }
    audit.check(
        template_names == {"default.md", "meeting.md", "lecture.md"},
        "Only the three public templates are included.",
        "The template inventory is not the three-file public set.",
    )

    forbidden: list[str] = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        lowered = path.name.lower()
        parts = {part.lower() for part in path.relative_to(root).parts}
        if parts & {".git", "tests", "__pycache__", ".uv-cache"}:
            forbidden.append(rel)
        elif lowered in {".env", "auth.bin", "direct_url.json", ".ds_store"}:
            forbidden.append(rel)
        elif lowered.endswith((".db", ".sqlite", ".sqlite3", ".pyc", ".pyo")):
            forbidden.append(rel)
        elif lowered.endswith(".pth") and lowered != "python313._pth":
            forbidden.append(rel)
        elif lowered.endswith((".mp3", ".m4a", ".wav", ".aac", ".flac", ".mp4", ".mov")):
            forbidden.append(rel)
    audit.check(
        not forbidden,
        "No private state, caches, tests, or recordings are bundled.",
        f"Forbidden bundled files: {', '.join(forbidden[:8])}",
    )

    pe_files = [path for path in files if path.suffix.lower() in PE_SUFFIXES]
    wrong_pe = [
        path.relative_to(root).as_posix() for path in pe_files if pe_machine(path) != 0x8664
    ]
    audit.check(
        bool(pe_files) and not wrong_pe,
        "Every PE executable/library is x86_64.",
        f"Non-x64 or invalid PE files: {', '.join(wrong_pe[:8])}",
    )

    sensitive: list[str] = []
    for path in files:
        suffix = "._pth" if path.name.endswith("._pth") else path.suffix.lower()
        if suffix not in TEXT_SUFFIXES:
            continue
        payload = path.read_bytes()
        if SECRET_PATTERN.search(payload) or PRIVATE_PATTERN.search(payload):
            sensitive.append(path.relative_to(root).as_posix())
    audit.check(
        not sensitive,
        "No credential values or developer paths were detected in text assets.",
        f"Sensitive text patterns found in: {', '.join(sensitive[:8])}",
    )

    server_source = (root / "app" / "windows_app" / "server.py").read_text(encoding="utf-8")
    browser_source = (root / "app" / "windows_app" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    launcher_source = (root / "app" / "windows_app" / "launcher.py").read_text(encoding="utf-8")
    runtime_paths = (root / "runtime" / "python313._pth").read_text(encoding="ascii")
    start_command = (root / "Start Plaud Community.cmd").read_text(encoding="ascii")
    audit.check(
        all(
            marker in server_source
            for marker in (
                "127.0.0.1",
                "X-Plaud-Session",
                "compare_digest",
                "Content-Security-Policy",
                "no-store",
            )
        )
        and "location.hash" in browser_source
        and "history.replaceState" in browser_source,
        "Loopback UI requires a fragment-delivered session token and restrictive headers.",
        "The loopback session-token security markers are incomplete.",
    )
    audit.check(
        all(
            marker in launcher_source
            for marker in ("PLAUD_AUTH_BLOB_FILE", "PLAUD_AUTO_REFRESH", "community-windows-lite")
        ),
        "Windows runtime uses an isolated Community Lite namespace.",
        "Windows namespace isolation markers are incomplete.",
    )
    audit.check(
        runtime_paths.splitlines()
        == ["python313.zip", ".", "Lib\\site-packages", "..\\app", "import site"]
        and "pythonw.exe" in start_command
        and " -I -B -m windows_app" in start_command,
        "Embedded Python path and launcher flags enforce isolated startup.",
        "Embedded Python path or isolated launcher flags are incorrect.",
    )

    if audit.failures:
        print(
            f"\nWindows release audit failed with {len(audit.failures)} issue(s).", file=sys.stderr
        )
        return 1
    print("\nWindows static release audit passed; native execution remains a separate gate.")
    return 0


def safe_extract(archive_path: Path, destination: Path) -> Path | None:
    with zipfile.ZipFile(archive_path) as archive:
        names: set[str] = set()
        roots: set[str] = set()
        for item in archive.infolist():
            pure = PurePosixPath(item.filename)
            if pure.is_absolute() or ".." in pure.parts or not pure.parts:
                print(f"[FAIL] Unsafe ZIP member: {item.filename}", file=sys.stderr)
                return None
            folded = item.filename.rstrip("/").casefold()
            if folded in names:
                print(f"[FAIL] Duplicate case-folded ZIP member: {item.filename}", file=sys.stderr)
                return None
            names.add(folded)
            roots.add(pure.parts[0])
            mode = (item.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                print(f"[FAIL] Symbolic link in ZIP: {item.filename}", file=sys.stderr)
                return None
        if roots != {APP_NAME}:
            print(f"[FAIL] ZIP root must be exactly {APP_NAME!r}.", file=sys.stderr)
            return None
        archive.extractall(destination)
    print("[PASS] ZIP members are traversal-safe, unique, and contain no symbolic links.")
    return destination / APP_NAME


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: audit-windows-release.py PATH", file=sys.stderr)
        return 64
    target = Path(argv[1]).resolve()
    if target.is_dir():
        return audit_directory(target)
    if target.suffix.lower() != ".zip" or not target.is_file():
        print("error: PATH must be a staged directory or ZIP", file=sys.stderr)
        return 64
    with tempfile.TemporaryDirectory(prefix="plaud-windows-audit-") as temporary:
        extracted = safe_extract(target, Path(temporary))
        return 1 if extracted is None else audit_directory(extracted)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
