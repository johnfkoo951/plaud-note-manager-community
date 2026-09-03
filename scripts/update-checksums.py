#!/usr/bin/env python3
"""Regenerate SHA256SUMS for every top-level release ZIP in dist/."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: update-checksums.py DIST_DIR", file=sys.stderr)
        return 64
    dist = Path(argv[1])
    archives = sorted(dist.glob("Plaud Note Manager Community-*.zip"))
    if not archives:
        print("no release ZIP files found", file=sys.stderr)
        return 1
    content = "".join(f"{digest(path)}  {path.name}\n" for path in archives)
    output = dist / "SHA256SUMS"
    temporary = output.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
