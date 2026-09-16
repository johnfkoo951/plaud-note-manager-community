**🇬🇧 English** · [🇰🇷 한국어](README.ko.md)

# Plaud Note Manager Community

A macOS and Windows workshop app that syncs your Plaud recording list and the
transcripts and summaries Plaud generated, then lets you find them again
locally. This is a reduced edition, kept separate from the personal
development build's credential store, data folder, and browser session.

> An unofficial community project. Not affiliated with or endorsed by Plaud.
> Use it only with your own account and recordings you are entitled to process.
> Changes to the Plaud web API may break it.

## What the shared edition covers

On both operating systems:

- Recording metadata sync
- User-initiated transcript and summary cache fetches
- Local search and Markdown export
- Five usage states and manual tags that are never written to Plaud Cloud
- Preview-first automatic routing, limited to folders that already exist in
  your own Plaud account
- An apply guard that moves only the items you selected, into the exact
  targets of a saved preview
- An undo guard that restores the exact previous folders, and only while the
  Cloud and local state are unchanged since the apply
- Two-stage recovery guidance for folder undo and interrupted applies, which
  survives a restart
- Optional ElevenLabs Scribe v2 re-transcription, run on your own API key,
  with ambiguous paid retries blocked
- Optional AI arbitration using a Claude or Codex app login, or your personal
  Claude, Codex, Gemini, or Grok API key

macOS only:

- In-app Plaud Web Login with a dedicated WebKit session
- A clipboard or direct-paste cURL fallback for when Web Login stalls or your
  SSO is not compatible
- Playback, local starring, and explicitly triggered folder and title cleanup
- Pre-expiry token refresh when the refresh material was captured

Windows Community Lite boundaries:

- Opens a `127.0.0.1` local screen in your default browser and protects the
  API with a random session token
- If the visible browser screen's auth heartbeat stops for two minutes, the
  local server finishes its work and shuts itself down
- Connects using an API cURL copied from your own Plaud web session
- Folder routing previews up to 200 recent unfiled recordings, applies only
  what you explicitly selected, and can safely undo the most recent apply
- Excludes audio playback, in-app Web Login, manual title and folder editing,
  and automatic token refresh

Not included:

- Obsidian or personal vault integration
- Scanning Chrome profiles, shell configuration, or host API keys
- Automatic full-library backfill right after login
- External AI calls, audio uploads, or automatic Cloud moves without consent
- Creating new folders during automatic classification, or any personal folder
  taxonomy baked into the app

The Claude and Codex CLI modes use the session you signed into in each
provider's own app; this app never reads or stores their OAuth tokens. Gemini
and Grok are API-key only in the Community edition, because a safe no-tool
stdin CLI boundary has not been confirmed for them. Every AI preview asks for
external-transfer confirmation and makes at most 20 calls.

## Privacy boundary

- macOS credentials live in the dedicated Keychain service
  `com.cmdspace.PlaudNoteManagerCommunity.auth`.
- Windows credentials live in `auth.bin`, encrypted with DPAPI and bound to the
  current Windows user. There is no plaintext fallback.
- The macOS cache is written only to
  `~/Library/Application Support/com.cmdspace.PlaudNoteManagerCommunity/`.
- The Windows cache is written only to
  `%LOCALAPPDATA%\CMDSPACE\PlaudNoteManagerCommunityLite\`.
- External provider API keys are stored separately from Plaud credentials, in
  their own macOS Keychain entry or Windows DPAPI ciphertext, and are never
  displayed again.
- The personal development build's database, Keychain, WebKit session, `.env`,
  and shell API keys are never read.
- The bundled Python runs in isolated mode (`-I -B`), with the runtime it needs
  inside the distributed files.
- Only HTTPS `api*.plaud.ai` hosts are allowed, and API requests do not follow
  redirects.

See the [privacy notes](docs/PRIVACY-KR.md) and [uninstall guide](docs/UNINSTALL-KR.md)
for detail. The follow-up design and verification conditions for the optional
Chrome auth bridge are kept separately in its [proposal](docs/CHROME-AUTH-BRIDGE-PROPOSAL.md).

## Install

Pick the ZIP for your operating system.

- Apple silicon Mac on macOS 14 or later: `macOS-arm64.zip`
- Intel Mac on macOS 14 or later: `macOS-x86_64.zip`
- 64-bit Windows 11: `Windows-x64.zip`
- An internet connection and your own Plaud account

On macOS, unzip and move the app to `Applications`. On Windows, extract the
whole ZIP and run `Start Plaud Community.cmd`. The current artifacts are
**not** Apple-notarized or Windows code-signed. Do not disable your operating
system's security features globally; run only files whose origin and SHA-256
you have checked. Step-by-step instructions are in the
[install guide](docs/INSTALL-KR.md).

## First run

1. On macOS, sign in with the app's **Plaud Web Login**. If the built-in login
   stalls, use **Import Copied cURL** or manual paste in the same auth window.
   On Windows, follow the prompt to copy your Plaud API cURL into the local
   screen.
2. Press **Sync** to fetch the recording list only.
3. Press **Backfill** only when you need it, to cache Plaud transcripts and
   summaries locally.
4. Search titles, transcripts, and summaries from the search field.
5. Organize per-recording usage state and manual tags in the Community-only
   local database.
6. In **automatic folder cleanup**, run a local-only or AI preview, then apply
   only the suggestions above 60% confidence that are actually right.
7. Press **Transcribe with ElevenLabs…** for individual recordings only,
   confirming the audio upload and possible cost each time.

Manual cURL is a fallback that fetches the current access token; it cannot
create new automatic refresh material. Without existing refresh material for
the same workspace, you will need a fresh cURL once the token expires.
Pre-expiry automatic refresh continues only when macOS Web Login captured that
material successfully.

Backfill downloads the transcripts and summaries in your account that are not
cached yet. Do not use it on a shared computer. For external provider setup and
data boundaries, see the [folder routing guide](docs/AUTO-FOLDER-ROUTING.md),
the [ElevenLabs transcription guide](docs/ELEVENLABS-TRANSCRIPTION.md), and the
[privacy notes](docs/PRIVACY-KR.md).

## Verify and build from source

```bash
uv sync --group dev
PLAUD_SECRET_STORE=test-file PLAUD_AUTO_REFRESH=0 uv run pytest -q
uv run ruff check .
uv run ruff format --check .
node --check windows_app/static/app.js
swift test --package-path app --scratch-path /private/tmp/plaud-community-swift-test --disable-sandbox --disable-automatic-resolution
swift build --package-path app -c release
swift build --package-path app --triple x86_64-apple-macosx14.0 -c release
# run against a source-only snapshot made with git archive after committing
scripts/audit-source.sh /path/to/source-snapshot
scripts/package-macos-app.sh
scripts/package-macos-intel.sh
python3 scripts/package-windows-portable.py
```

Packaging requires a clean Git state and writes the app, the ZIPs, and
`SHA256SUMS` into `dist/`. The Windows script bundles a pinned official CPython
x64 runtime with Windows wheels, then runs static architecture and privacy
audits. The native Windows self-test has to pass separately, through
`.github/workflows/build-windows.yml` or on a real Windows 11 PC.

## Known distribution limits

- macOS arm64 is built and CLI smoke-tested on Apple silicon; x86_64 is built
  under Rosetta on Apple silicon. GUI verification on real Intel hardware is a
  separate release gate.
- The Windows ZIP can be cross-built statically, but before it is handed out it
  must pass the bundled `--self-test` and a DPAPI round-trip on Windows 11 x64.
- Developer ID signing with Apple notarization, and Windows Authenticode
  signing, are separate release steps that the current artifacts have not been
  through.
- Real Plaud account login, token refresh, and sync have to be verified by each
  participant on their own account.
- The Plaud web API is not an official public SDK, so server-side changes may
  require fixes here.

Test coverage and what remains unverified are recorded separately in the
[testing log](docs/TESTING.md).

## License

The project code is Apache-2.0. The copyright holder name in `LICENSE` is a
public copyright notice, not personal information about app users. Bundled
third-party software follows [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

---

By [Yohan Koo (CMDSPACE)](https://cmdspace.work).
