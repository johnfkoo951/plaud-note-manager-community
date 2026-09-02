# Verification Record

This file distinguishes deterministic checks from live external side effects. Update it for every release artifact.

## Required deterministic gates

```bash
PLAUD_SECRET_STORE=test-file PLAUD_AUTO_REFRESH=0 uv run pytest -q
uv run ruff check .
uv run ruff format --check .
swift build --package-path app -c release
scripts/audit-source.sh
scripts/package-macos-app.sh
```

The release audit must verify:

- arm64 executable and macOS 14 deployment target
- exact Community Bundle ID and distribution profile
- valid nested and outer code signatures
- embedded Python can import and launch the CLI with `-I -B`
- no `.env`, database, log, VCS, test, cache, or source fixture in the app
- no developer home path, private vault name, credential pattern, or token-like fixture in the app strings/files
- ZIP SHA-256 recorded next to the artifact

## Live gates that require a test Plaud account

- first Web Login on a fresh macOS user
- metadata sync, one detail fetch, search, playback, and export
- token refresh after the access token approaches expiry
- Community app cannot see the private edition's Keychain, DB, or WebKit session
- no network request before the user initiates login
- network destinations remain limited to Plaud web/API hosts during core use
- disconnect removes only Community credentials; clearing Web Session and local data work independently

Do not mark live gates complete based only on unit tests or a successful build.

## Distribution gates not provided by the local workshop build

- Developer ID Application signature
- Apple notarization and stapling
- clean-Mac Gatekeeper acceptance without Control-click
- Intel Mac support

## Release result

Populate after running the gates:

- Source commit: pending
- Python tests: pending
- Ruff check/format: pending
- Swift release build: pending
- Source privacy scan: pending
- Packaged app audit: pending
- Live Plaud E2E: requires user/test account
- Notarization: not performed

self_docked: A passing deterministic suite does not prove the unofficial Plaud API will remain compatible or that a real account flow succeeds.
