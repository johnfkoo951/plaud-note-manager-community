#!/usr/bin/env python3
"""Create a modern PNG-backed ICNS container without invoking iconutil."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

CHUNKS = (
    (b"icp4", "icon_16.png"),
    (b"icp5", "icon_32.png"),
    (b"icp6", "icon_64.png"),
    (b"ic07", "icon_128.png"),
    (b"ic08", "icon_256.png"),
    (b"ic09", "icon_512.png"),
    (b"ic10", "icon_1024.png"),
)


def build(input_dir: Path, output: Path) -> None:
    elements: list[bytes] = []
    for kind, name in CHUNKS:
        data = (input_dir / name).read_bytes()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"not a PNG file: {name}")
        elements.append(kind + struct.pack(">I", len(data) + 8) + data)
    body = b"".join(elements)
    output.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: make-icns.py INPUT_DIR OUTPUT.icns", file=sys.stderr)
        return 64
    build(Path(argv[1]), Path(argv[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
