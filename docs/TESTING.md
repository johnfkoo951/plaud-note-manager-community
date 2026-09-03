# Verification Record

This file distinguishes deterministic checks from live external side effects. Update it for every release artifact.

## Required deterministic gates

```bash
PLAUD_SECRET_STORE=test-file PLAUD_AUTO_REFRESH=0 uv run pytest -q
uv run ruff check .
uv run ruff format --check .
swift build --package-path app -c release
swift build --package-path app --triple x86_64-apple-macosx14.0 -c release
scripts/audit-source.sh
scripts/package-macos-app.sh
scripts/package-macos-intel.sh
python3 scripts/package-windows-portable.py
```

The release audit must verify:

- exact arm64 or x86_64 Mach-O architecture and macOS 14 deployment target
- exact Community Bundle ID and distribution profile
- valid nested and outer code signatures
- embedded Python can import and launch the CLI with `-I -B`
- no `.env`, database, log, VCS, test, cache, or source fixture in the app
- no developer home path, private vault name, credential pattern, or token-like fixture in the app strings/files
- ZIP SHA-256 recorded next to the artifact

The Windows static release audit must verify:

- official CPython x64 archive hash and locked binary wheels
- every bundled PE executable, DLL, and extension module is x86_64
- no `.env`, `auth.bin`, database, recording, test, cache, or developer path is bundled
- exactly the three public templates are included
- loopback-only server, fragment-delivered session token, authenticated API header, CSP, and `no-store` markers remain present
- ZIP members have one expected root, no traversal, case-fold collision, or symbolic link

The packaged Windows native self-test must run on Windows 11 x64. It checks the HTTP session boundary and a 16 KiB synthetic DPAPI encrypt/read/delete round trip. Running it on macOS can check the HTTP path but must report DPAPI as skipped.

## Live gates that require a test Plaud account

- first Web Login on a fresh macOS user
- first cURL import on a fresh Windows user
- metadata sync, one detail fetch, search, playback, and export
- token refresh after the access token approaches expiry
- Community app cannot see the private edition's Keychain, DB, or WebKit session
- no network request before the user initiates login
- network destinations remain limited to Plaud web/API hosts during core use
- disconnect removes only Community credentials; clearing Web Session and local data work independently
- Windows browser extensions cannot be proven safe by the app; test with a trusted clean profile

Do not mark live gates complete based only on unit tests or a successful build.

## Distribution gates not provided by the local workshop build

- Developer ID Application signature
- Apple notarization and stapling
- clean-Mac Gatekeeper acceptance without Control-click
- Windows Authenticode signature and reputation
- actual Intel Mac GUI and actual Windows 11 participant-machine E2E

## Current source-gate result — 2026-09-03

- Python tests: 215 passed
- Ruff check/format: passed
- JavaScript syntax check: passed
- Swift arm64 release build, macOS minimum 14.0: passed
- Swift x86_64 release build, macOS minimum 14.0: passed
- Source privacy scan: passed
- Gitleaks 8.30.1 working-tree scan: no leaks found
- Packaged artifact audits and SHA-256: recorded next to the generated ZIPs in `dist/RELEASE-REPORT.md`
- Windows x64 native HTTP/DPAPI self-test: workflow and packaged diagnostic prepared; not run on Windows in this local session
- Live Plaud E2E: requires a fresh user and a dedicated test account
- Apple notarization and Windows code signing: not performed

self_docked: A passing deterministic suite does not prove the unofficial Plaud API will remain compatible or that a real account flow succeeds.
