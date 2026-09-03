#!/usr/bin/env python3
"""Build a privacy-audited Windows 11 x64 portable archive from any host."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import NoReturn

APP_NAME = "Plaud Note Manager Community"
PYTHON_VERSION = "3.13.15"
PYTHON_ARCHIVE = f"python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{PYTHON_ARCHIVE}"
PYTHON_SHA256 = "d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf"
WINDOWS_PLATFORM = "x86_64-pc-windows-msvc"


def fail(message: str) -> NoReturn:
    raise SystemExit(f"error: {message}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    if not match:
        fail("could not read the project version")
    return match.group(1)


def git_output(root: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode:
        fail(f"git {' '.join(args)} failed")
    return process.stdout.strip()


def require_clean_source(root: Path) -> str:
    dirty = git_output(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        ".",
        ":(exclude)dist",
    )
    if dirty:
        fail("commit or stash source changes before packaging")
    return git_output(root, "rev-parse", "--short", "HEAD")


def download_runtime(cache_file: Path) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    if cache_file.exists() and sha256(cache_file) == PYTHON_SHA256:
        print(f"Using verified CPython cache: {cache_file.name}")
        return
    cache_file.unlink(missing_ok=True)
    temporary = cache_file.with_suffix(".download")
    temporary.unlink(missing_ok=True)
    print(f"Downloading official CPython {PYTHON_VERSION} Windows x64 runtime...")
    request = urllib.request.Request(PYTHON_URL, headers={"User-Agent": "PlaudCommunityBuilder/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as out:
            shutil.copyfileobj(response, out)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        fail(f"could not download CPython runtime: {exc}")
    actual = sha256(temporary)
    if actual != PYTHON_SHA256:
        temporary.unlink(missing_ok=True)
        fail(f"CPython archive checksum mismatch: {actual}")
    temporary.replace(cache_file)


def copy_tree_without_caches(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store", "tests"),
    )


def install_windows_dependencies(root: Path, site_packages: Path, cache_dir: Path) -> None:
    environment = os.environ.copy()
    environment["UV_CACHE_DIR"] = str(cache_dir)
    command = [
        "uv",
        "pip",
        "install",
        "--target",
        str(site_packages),
        "--python-version",
        "3.13",
        "--python-platform",
        WINDOWS_PLATFORM,
        "--only-binary",
        ":all:",
        "--no-deps",
        "--require-hashes",
        "--requirement",
        str(root / "requirements-windows.txt"),
    ]
    print("Installing locked Windows wheels...")
    process = subprocess.run(command, cwd=root, env=environment, check=False)
    if process.returncode:
        fail("uv could not install the Windows dependency set")


def remove_runtime_noise(runtime: Path) -> None:
    for path in sorted(runtime.rglob("*"), reverse=True):
        if path.is_file() and (
            path.suffix.lower() in {".pyc", ".pyo", ".pth"}
            or path.name in {"direct_url.json", "REQUESTED"}
        ):
            path.unlink()
        elif path.is_dir() and path.name in {"__pycache__", "sboms", "bin"}:
            shutil.rmtree(path)
    for path in sorted(runtime.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def write_ascii(path: Path, content: str) -> None:
    path.write_bytes(content.replace("\n", "\r\n").encode("ascii"))


def stage_release(root: Path, stage: Path, version: str, commit: str, runtime_zip: Path) -> None:
    app_dir = stage / "app"
    runtime = stage / "runtime"
    app_dir.mkdir(parents=True)
    runtime.mkdir(parents=True)

    with zipfile.ZipFile(runtime_zip) as archive:
        archive.extractall(runtime)

    site_packages = runtime / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    install_windows_dependencies(root, site_packages, stage / ".uv-cache")
    remove_runtime_noise(runtime)
    shutil.rmtree(stage / ".uv-cache", ignore_errors=True)

    copy_tree_without_caches(root / "core", app_dir / "core")
    copy_tree_without_caches(root / "windows_app", app_dir / "windows_app")
    templates = app_dir / "templates"
    templates.mkdir()
    for name in ("default.md", "meeting.md", "lecture.md"):
        shutil.copy2(root / "templates" / name, templates / name)

    shutil.copy2(root / "LICENSE", app_dir / "LICENSE")
    shutil.copy2(root / "THIRD_PARTY_NOTICES.md", app_dir / "THIRD_PARTY_NOTICES.md")
    python_license = runtime / "LICENSE.txt"
    if not python_license.is_file():
        fail("the official CPython archive did not include LICENSE.txt")
    shutil.copy2(python_license, app_dir / "PYTHON_LICENSE.txt")

    (runtime / "python313._pth").write_text(
        "python313.zip\n.\nLib\\site-packages\n..\\app\nimport site\n",
        encoding="ascii",
    )
    write_ascii(
        stage / "Start Plaud Community.cmd",
        '@echo off\nsetlocal\ncd /d "%~dp0"\n'
        'start "" /D "%~dp0" "%~dp0runtime\\pythonw.exe" -I -B -m windows_app\n',
    )
    write_ascii(
        stage / "Diagnose Plaud Community.cmd",
        '@echo off\nsetlocal\ncd /d "%~dp0"\n'
        '"%~dp0runtime\\python.exe" -I -B -m windows_app --self-test\n'
        "echo.\necho Press any key to close.\npause >nul\n",
    )
    (stage / "README-WINDOWS.txt").write_text(
        f"""Plaud Note Manager Community {version} - Windows 11 x64

1. Keep this entire folder together in a location you control.
2. Double-click Start Plaud Community.cmd.
3. Windows opens the local interface in your default browser.
4. Paste only an API cURL copied from your own Plaud Web session.

Credentials are encrypted for the current Windows account with DPAPI and saved
under LocalAppData. The app reads Plaud Cloud data; it does not rename, delete,
or otherwise edit cloud recordings. Backfill runs only after you press its button.

This unsigned community build may trigger Windows reputation protection. Do not
disable SmartScreen or Smart App Control. Use it only when you received this
archive from the workshop organizer and its SHA-256 matches SHA256SUMS.

To inspect the local runtime without connecting an account, run
Diagnose Plaud Community.cmd. Native Windows and live Plaud account checks remain
separate release gates documented by the organizer.
""",
        encoding="utf-8",
    )
    (stage / "BUILD-INFO.txt").write_text(
        "\n".join(
            (
                f"name={APP_NAME}",
                f"version={version}",
                f"source_commit={commit}",
                "distribution_profile=community-windows-lite",
                "operating_system=Windows 11",
                "architecture=x86_64",
                f"python_version={PYTHON_VERSION}",
                f"python_source={PYTHON_URL}",
                f"python_sha256={PYTHON_SHA256}",
                "",
            )
        ),
        encoding="ascii",
    )


def make_zip(stage: Path, archive_path: Path) -> None:
    temporary = archive_path.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        root_parent = stage.parent
        for path in sorted(stage.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if path.is_dir():
                continue
            arcname = path.relative_to(root_parent).as_posix()
            out.write(path, arcname)
    temporary.replace(archive_path)


def backup_existing(archive: Path) -> None:
    if not archive.exists():
        return
    backup_root = archive.parent / "backups"
    suffix = 1
    candidate = backup_root / f"{archive.stem}-previous{archive.suffix}"
    while candidate.exists():
        suffix += 1
        candidate = backup_root / f"{archive.stem}-previous-{suffix}{archive.suffix}"
    backup_root.mkdir(parents=True, exist_ok=True)
    archive.replace(candidate)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    if shutil.which("uv") is None:
        fail("uv is required to build the Windows archive")
    version = project_version(root)
    commit = require_clean_source(root)
    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    runtime_zip = dist / ".cache" / PYTHON_ARCHIVE
    download_runtime(runtime_zip)
    final_zip = dist / f"{APP_NAME}-{version}-Windows-x64.zip"

    with tempfile.TemporaryDirectory(prefix="plaud-community-windows-") as temporary:
        stage = Path(temporary) / APP_NAME
        stage.mkdir()
        stage_release(root, stage, version, commit, runtime_zip)
        subprocess.run(
            [sys.executable, str(root / "scripts" / "audit-windows-release.py"), str(stage)],
            check=True,
        )
        backup_existing(final_zip)
        make_zip(stage, final_zip)

    subprocess.run(
        [sys.executable, str(root / "scripts" / "audit-windows-release.py"), str(final_zip)],
        check=True,
    )
    subprocess.run(
        [sys.executable, str(root / "scripts" / "update-checksums.py"), str(dist)],
        check=True,
    )
    print("\nWindows portable archive ready (static cross-build verification):")
    print(f"  {final_zip}")
    print(f"  sha256: {sha256(final_zip)}")
    print("  native Windows self-test: required before participant handout")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
