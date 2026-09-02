"""Parse a copied Plaud cURL into the macOS Keychain credential bundle.

Captures both the auth headers and the `cookie:` line so the embedded
WKWebView can be primed with the same session the cURL came from.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from urllib.parse import urlsplit

REQUIRED_HEADERS = {
    "authorization": "PLAUD_AUTHORIZATION",
    "x-device-id": "PLAUD_X_DEVICE_ID",
}

OPTIONAL_HEADERS = {
    # Legacy identity header. Current Plaud Web requests no longer send it,
    # but preserve it when importing an older capture.
    "x-pld-user": "PLAUD_X_PLD_USER",
    "x-pld-tag": "PLAUD_X_PLD_TAG",  # legacy; current web API omits it
    "app-language": "PLAUD_APP_LANGUAGE",
    "app-platform": "PLAUD_APP_PLATFORM",
    "edit-from": "PLAUD_EDIT_FROM",
    "origin": "PLAUD_ORIGIN",
    "referer": "PLAUD_REFERER",
    "timezone": "PLAUD_TIMEZONE",
}

DEFAULTS = {
    "PLAUD_BASE_URL": "https://api-apne1.plaud.ai",
    "PLAUD_APP_LANGUAGE": "en",
    "PLAUD_APP_PLATFORM": "web",
    "PLAUD_EDIT_FROM": "web",
    "PLAUD_ORIGIN": "https://web.plaud.ai",
    "PLAUD_REFERER": "https://web.plaud.ai/",
    "PLAUD_TIMEZONE": "Asia/Seoul",
}


def parse_curl(curl: str) -> dict[str, str]:
    """Return a flat dict suitable for atomic credential storage.

    Chrome may copy the command on one line or with ``\\`` continuations, and
    emits cookies as ``-b 'name=value'`` (without a ``cookie:`` prefix). Parse
    argv instead of individual lines so all of those current formats work.
    """
    out: dict[str, str] = dict(DEFAULTS)

    normalized = curl.replace("\\\r\n", " ").replace("\\\n", " ")
    try:
        tokens = shlex.split(normalized)
    except ValueError as exc:
        raise SystemExit(f"invalid cURL quoting: {exc}") from None
    if not tokens or Path(tokens[0]).name.lower() not in ("curl", "curl.exe"):
        raise SystemExit("input is not a cURL command copied from Plaud Web")

    request_url: str | None = None

    def capture_header(raw: str) -> None:
        if ":" not in raw:
            return
        key, _, val = raw.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "cookie":
            out["PLAUD_COOKIE"] = val
        elif key in REQUIRED_HEADERS:
            out[REQUIRED_HEADERS[key]] = val
        elif key in OPTIONAL_HEADERS:
            out[OPTIONAL_HEADERS[key]] = val

    index = 0
    while index < len(tokens):
        token = tokens[index]
        value: str | None = None

        if token in ("-H", "--header", "-b", "--cookie", "--url"):
            if index + 1 < len(tokens):
                index += 1
                value = tokens[index]
        elif token.startswith("--header="):
            value = token.partition("=")[2]
            token = "--header"
        elif token.startswith("--cookie="):
            value = token.partition("=")[2]
            token = "--cookie"
        elif token.startswith("--url="):
            value = token.partition("=")[2]
            token = "--url"
        elif token.startswith("-H") and len(token) > 2:
            value = token[2:]
            token = "-H"
        elif token.startswith("-b") and len(token) > 2:
            value = token[2:]
            token = "-b"

        if value is not None:
            if token in ("-H", "--header"):
                capture_header(value)
            elif token in ("-b", "--cookie"):
                cookie = value.partition(":")[2] if value.lower().startswith("cookie:") else value
                cookie = cookie.strip()
                if cookie:
                    out["PLAUD_COOKIE"] = cookie
            elif token == "--url":
                request_url = value
        elif request_url is None and token.startswith(("https://", "http://")):
            request_url = token

        index += 1

    parsed_url = urlsplit(request_url or "")
    host = (parsed_url.hostname or "").lower()
    if parsed_url.scheme != "https" or not (host.startswith("api") and host.endswith(".plaud.ai")):
        raise SystemExit("cURL must target an https://api-*.plaud.ai request")
    out["PLAUD_BASE_URL"] = f"{parsed_url.scheme}://{parsed_url.netloc}"

    missing = [v for v in REQUIRED_HEADERS.values() if v not in out]
    if missing:
        raise SystemExit(f"missing required headers in cURL: {', '.join(missing)}")
    return out


def write_env(values: dict[str, str], env_path: Path) -> None:
    # Credentials are committed as one macOS Keychain blob.  The common lock
    # prevents a cURL import from overwriting a concurrently rotated refresh
    # token; no plaintext backup is ever created.
    from core.secret_store import credential_lock, update_credential_values
    from core.ws_refresh import credential_env_updates

    with credential_lock(env_path):
        updates = credential_env_updates(values, env_path, already_locked=True)
        update_credential_values(updates, env_path, already_locked=True)
    print(f"wrote {env_path}")


def main(argv: list[str]) -> int:
    env_path = Path(argv[1]) if len(argv) > 1 else Path(".env")
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
