"""Parse a copied Plaud cURL into the Community credential bundle.

Captures both the auth headers and the `cookie:` line so the embedded
WKWebView can be primed with the same session the cURL came from.
"""

from __future__ import annotations

import sys
from pathlib import Path

from core.curl_auth import parse_curl as _parse_curl
from core.curl_auth import store_credentials


def parse_curl(curl: str) -> dict[str, str]:
    """CLI-compatible wrapper around the reusable parser."""

    try:
        return _parse_curl(curl)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


def write_env(values: dict[str, str], env_path: Path) -> None:
    # Credentials are committed as one native protected-store blob. The common lock
    # prevents a cURL import from overwriting a concurrently rotated refresh
    # token; no plaintext backup is ever created.
    store_credentials(values, env_path)
    print(f"wrote {env_path}")


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        env_path = Path(argv[1])
    else:
        from core.config import resolve_env_path

        env_path = resolve_env_path()
    curl_text = sys.stdin.read()
    if not curl_text.strip():
        print("paste your Plaud cURL on stdin (Ctrl-D to finish)", file=sys.stderr)
        return 1
    values = parse_curl(curl_text)
    write_env(values, env_path)
    has_cookie = "PLAUD_COOKIE" in values
    print(f"cookie captured: {has_cookie}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
