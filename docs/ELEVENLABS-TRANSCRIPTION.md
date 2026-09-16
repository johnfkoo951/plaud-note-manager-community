# ElevenLabs transcription mode

Plaud Note Manager Community can optionally create a second, local transcript
with ElevenLabs Scribe. This is an external-provider mode: it is off until the
user adds their own API key and explicitly approves each upload.

## Privacy and cost boundary

- The selected recording audio leaves the device and is uploaded to ElevenLabs.
- ElevenLabs credits or pay-as-you-go balance may be consumed.
- The API currently defaults to provider logging. ElevenLabs documents
  `enable_logging=false` zero-retention mode as an Enterprise-only feature, so
  Community does not claim zero retention for ordinary accounts.
- Audio is held in an app-private local temporary directory only during the
  request and is deleted on success, cancellation, and handled error paths. A
  forced power-off/SIGKILL can bypass cleanup; the next transcription removes
  only this app's regular MP3/Opus files older than six hours while holding an
  exclusive directory lock. It never follows links or scans unrelated temp
  files.
- The returned transcript and speaker segments are stored in the local
  Community SQLite database. They are not sent into the private CMDS/Obsidian
  pipeline.

## API key

Use an ElevenLabs key restricted to Speech to Text with a suitable credit
quota. The app stores it in a provider-specific macOS Keychain item or a
provider-specific Windows DPAPI-encrypted blob. It is never written to
`settings.env`, passed in command arguments, or returned as a masked preview.

- macOS: Keychain service `com.cmdspace.PlaudNoteManagerCommunity.auth.providers`, account `api-key:elevenlabs`
- Windows: `%LOCALAPPDATA%\CMDSPACE\PlaudNoteManagerCommunityLite\config\provider-secrets\elevenlabs.bin`

macOS example:

```sh
pbpaste | uv run plaud provider-key-set elevenlabs
uv run plaud provider-key-status elevenlabs
```

The packaged macOS and Windows interfaces expose the same operation in
Settings. Their UI sends the secret directly to the local process for protected
storage; it does not construct a command line containing the key.

Remove only that provider key:

```sh
uv run plaud provider-key-delete elevenlabs
```

Plaud **연결 해제**는 이 key를 지우지 않습니다. macOS Keychain 항목은 앱 삭제 후에도 남으므로 [삭제 안내](UNINSTALL-KR.md)에 따라 별도로 제거하세요.

## Run transcription

The confirmation flag is deliberately required. An existing local external
transcript also blocks a second paid upload unless `--force` is supplied.

Before the paid POST, the app durably records an opaque, hashed attempt marker.
It clears that marker only after a definite provider rejection or after the
transcript is safely stored. If a timeout, crash, malformed success response, or
uncertain local save leaves the outcome unknown, every interface blocks the next
upload. Retrying requires a stronger confirmation that the earlier request may
already have consumed credits and `--force` may therefore bill twice. The
marker contains no Plaud file ID, audio, transcript, or API key.

```sh
uv run plaud elevenlabs-transcribe FILE_ID --confirm-upload --language ko
```

The Community client uses synchronous `POST /v1/speech-to-text`, model
`scribe_v2`, word timestamps, and optional speaker diarization. ElevenLabs
supports 1-32 expected speakers. Although the provider currently accepts files
below 5 GB, Community applies a stricter 1 GiB download/upload ceiling and
bounded network timeouts. Plaud MP3 sources are uploaded as `audio/mpeg`; the
Opus fallback keeps its `.opus` filename and `audio/ogg` media type. Per-file
and private-temp lock acquisition fails after a bounded wait instead of keeping
the Windows background server alive indefinitely.

Official references checked 2026-09-03:

- [Create transcript API](https://elevenlabs.io/docs/api-reference/speech-to-text/convert)
- [Speech-to-text quickstart](https://elevenlabs.io/docs/eleven-api/guides/cookbooks/speech-to-text)
- [API-key security, scopes, quotas, and rotation](https://elevenlabs.io/docs/overview/administration/workspaces/api-keys)
