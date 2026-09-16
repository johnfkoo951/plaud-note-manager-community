from __future__ import annotations

import errno
import hashlib
import multiprocessing
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import cli.main as cli_main
import core.transcribe as transcribe
from cli.main import app
from core.client import TempAudioSource
from core.config import PlaudConfig
from core.storage import Storage
from core.transcribe import (
    TranscriptionError,
    TranscriptionOutcomeUnknown,
    transcribe_and_store,
    transcribe_file,
)


def _transcription_lock_worker(
    db_path: str,
    file_id: str,
    attempting,
    acquired,
    contended_observed,
    release,
    hold: bool,
) -> None:
    """Spawn-safe worker proving the lock is shared across processes."""

    attempting.set()
    with transcribe._transcription_lock(Path(db_path), file_id) as contended:
        if contended:
            contended_observed.set()
        acquired.set()
        if hold and not release.wait(timeout=10):
            raise RuntimeError("test lock release timed out")


def _config() -> PlaudConfig:
    return PlaudConfig(
        base_url="https://api.plaud.ai",
        authorization="bearer test-plaud-token",
        x_device_id="test-device",
    )


class FakePlaudClient:
    def __init__(self, _cfg: PlaudConfig) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def temp_audio_source(self, file_id: str) -> TempAudioSource:
        assert file_id == "recording-1"
        return TempAudioSource(
            url="https://audio.example.test/recording.mp3",
            filename="recording.mp3",
            content_type="audio/mpeg",
        )


def _capture_tempfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, suffix: str = ".mp3"
) -> Path:
    target = tmp_path / f"bounded-upload{suffix}"
    monkeypatch.setattr(transcribe, "_temporary_audio_directory", lambda: tmp_path)

    def mkstemp(*, dir: Path, prefix: str, suffix: str):
        assert Path(dir) == tmp_path
        assert prefix == "plaud-community-"
        assert suffix == target.suffix
        fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        return fd, str(target)

    monkeypatch.setattr(transcribe.tempfile, "mkstemp", mkstemp)
    return target


def _mock_http(
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> None:
    real_client = httpx.Client
    transport = httpx.MockTransport(handler)

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(transcribe.httpx, "Client", client)


def test_transcribe_requires_explicit_upload_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe, "get_api_key", lambda _provider: "unused-secret")

    with pytest.raises(TranscriptionError, match="confirmation required"):
        transcribe_file(_config(), "recording-1", confirm_upload=False)


def test_transcribe_missing_key_stops_before_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe, "get_api_key", lambda _provider: None)
    monkeypatch.setattr(
        transcribe,
        "PlaudClient",
        lambda _cfg: pytest.fail("Plaud must not be called without a provider key"),
    )

    with pytest.raises(TranscriptionError, match="not configured"):
        transcribe_file(_config(), "recording-1", confirm_upload=True)


def test_transcribe_rejects_file_id_path_injection_before_key_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        transcribe,
        "get_api_key",
        lambda _provider: pytest.fail("key store must not be read for an invalid id"),
    )

    with pytest.raises(ValueError, match="invalid Plaud file id"):
        transcribe_file(_config(), "../recording", confirm_upload=True)


def test_transcribe_uploads_and_persists_without_leaving_temp_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_key = "test-elevenlabs-secret-never-print"
    temp_audio = _capture_tempfile(monkeypatch, tmp_path)
    monkeypatch.setattr(transcribe, "get_api_key", lambda _provider: provider_key)
    monkeypatch.setattr(transcribe, "PlaudClient", FakePlaudClient)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=b"fake-mp3-audio", request=request)
        assert request.url == httpx.URL(transcribe.ELEVENLABS_STT_URL)
        seen["api_key"] = request.headers.get("xi-api-key")
        body = request.read()
        seen["body"] = body
        return httpx.Response(
            200,
            json={
                "language_code": "ko",
                "language_probability": 0.99,
                "text": "안녕하세요 세계",
                "words": [
                    {
                        "type": "word",
                        "text": "안녕하세요",
                        "speaker_id": "speaker_0",
                        "start": 0.0,
                        "end": 0.5,
                    },
                    {
                        "type": "word",
                        "text": "세계",
                        "speaker_id": "speaker_0",
                        "start": 0.6,
                        "end": 1.0,
                    },
                ],
            },
            request=request,
        )

    _mock_http(monkeypatch, handler)
    storage = Storage(tmp_path / "community.db")

    result = transcribe_and_store(
        _config(),
        "recording-1",
        storage=storage,
        confirm_upload=True,
        language_code="ko",
        num_speakers=2,
        now=123,
    )

    assert seen["api_key"] == provider_key
    assert b"scribe_v2" in seen["body"]
    assert b"recording.mp3" in seen["body"]
    assert result["text"] == "안녕하세요 세계"
    assert result["audio_bytes_uploaded"] == len(b"fake-mp3-audio")
    row = storage.get_cmds_transcript("recording-1")
    assert row is not None
    assert row["model"] == "scribe_v2"
    assert row["text"] == "안녕하세요 세계"
    assert not temp_audio.exists()


def test_opus_fallback_keeps_truthful_filename_and_content_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class OpusPlaudClient(FakePlaudClient):
        def temp_audio_source(self, file_id: str) -> TempAudioSource:
            assert file_id == "recording-1"
            return TempAudioSource(
                url="https://audio.example.test/recording.opus",
                filename="recording.opus",
                content_type="audio/ogg",
            )

    temp_audio = _capture_tempfile(monkeypatch, tmp_path, suffix=".opus")
    monkeypatch.setattr(transcribe, "get_api_key", lambda _provider: "test-secret")
    monkeypatch.setattr(transcribe, "PlaudClient", OpusPlaudClient)
    uploaded: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=b"fake-opus-audio", request=request)
        uploaded["body"] = request.read()
        return httpx.Response(200, json={"text": "opus result", "words": []}, request=request)

    _mock_http(monkeypatch, handler)
    result = transcribe_and_store(
        _config(),
        "recording-1",
        storage=Storage(tmp_path / "community.db"),
        confirm_upload=True,
    )

    assert result["text"] == "opus result"
    assert b'filename="recording.opus"' in uploaded["body"]
    assert b"Content-Type: audio/ogg" in uploaded["body"]
    assert not temp_audio.exists()


def test_ambiguous_upload_leaves_private_marker_and_requires_force_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_key = "test-secret"
    _capture_tempfile(monkeypatch, tmp_path)
    monkeypatch.setattr(transcribe, "get_api_key", lambda _provider: provider_key)
    monkeypatch.setattr(transcribe, "PlaudClient", FakePlaudClient)
    post_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls
        if request.method == "GET":
            return httpx.Response(200, content=b"fake-mp3-audio", request=request)
        post_calls += 1
        if post_calls == 1:
            raise httpx.ReadTimeout("provider response timed out", request=request)
        return httpx.Response(200, json={"text": "forced retry", "words": []}, request=request)

    _mock_http(monkeypatch, handler)
    storage = Storage(tmp_path / "community.db")

    with pytest.raises(TranscriptionOutcomeUnknown, match="outcome is unknown"):
        transcribe_and_store(_config(), "recording-1", storage=storage, confirm_upload=True)

    marker = transcribe._transcription_attempt_path(storage._db_path, "recording-1")
    assert marker.is_file()
    assert "recording-1" not in str(marker)
    if os.name == "posix":
        assert stat.S_IMODE(marker.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(marker.stat().st_mode) == 0o600

    with pytest.raises(TranscriptionError, match="may bill twice"):
        transcribe_and_store(_config(), "recording-1", storage=storage, confirm_upload=True)
    assert post_calls == 1

    result = transcribe_and_store(
        _config(),
        "recording-1",
        storage=storage,
        confirm_upload=True,
        force=True,
    )
    assert result["text"] == "forced retry"
    assert post_calls == 2
    assert not marker.exists()


def test_windows_attempt_marker_uses_write_through_without_opening_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = transcribe._transcription_attempt_path(tmp_path / "community.db", "recording-1")
    moves: list[tuple[Path, Path, bool]] = []
    real_open = os.open

    def guarded_open(path, flags, mode=0o777):
        if Path(path) == marker.parent:
            pytest.fail("Windows marker durability must not open a directory")
        return real_open(path, flags, mode)

    def move_file(source: Path, destination: Path, *, replace_existing: bool) -> None:
        moves.append((source, destination, replace_existing))
        os.replace(source, destination)

    monkeypatch.setattr(transcribe, "_is_windows_runtime", lambda: True)
    monkeypatch.setattr(transcribe, "_move_file_windows", move_file)
    monkeypatch.setattr(transcribe.os, "open", guarded_open)

    transcribe._write_attempt_marker(marker, model_id="scribe_v2")
    assert marker.is_file()
    transcribe._clear_attempt_marker(marker)

    assert not marker.exists()
    assert [replace for _source, _destination, replace in moves] == [True, False]


def test_private_temp_audio_scavenges_only_stale_owned_regular_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "private-audio"
    directory.mkdir(mode=0o700)
    stale = directory / "plaud-community-stale.mp3"
    stale.write_bytes(b"stale")
    os.utime(stale, (1, 1))
    fresh = directory / "plaud-community-fresh.opus"
    fresh.write_bytes(b"fresh")
    unrelated = directory / "other-audio.mp3"
    unrelated.write_bytes(b"keep")
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    linked = directory / "plaud-community-linked.mp3"
    linked.symlink_to(outside)
    monkeypatch.setattr(transcribe, "_temporary_audio_directory", lambda: directory)

    with transcribe._temporary_audio(suffix=".mp3") as active:
        assert active.parent == directory
        assert active.is_file()
        assert not stale.exists()
        assert fresh.is_file()
        assert unrelated.is_file()
        assert linked.is_symlink()
        assert outside.read_bytes() == b"outside"

    assert not active.exists()
    if os.name == "posix":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_provider_error_deletes_temp_and_never_discloses_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_key = "test-do-not-leak-this-key"
    temp_audio = _capture_tempfile(monkeypatch, tmp_path)
    monkeypatch.setattr(transcribe, "get_api_key", lambda _provider: provider_key)
    monkeypatch.setattr(transcribe, "PlaudClient", FakePlaudClient)

    def handler(request: httpx.Request) -> httpx.Response:
        status = 200 if request.method == "GET" else 401
        return httpx.Response(status, content=b"provider-detail", request=request)

    _mock_http(monkeypatch, handler)

    with pytest.raises(TranscriptionError) as caught:
        transcribe_file(_config(), "recording-1", confirm_upload=True)

    assert "HTTP 401" in str(caught.value)
    assert provider_key not in str(caught.value)
    assert "provider-detail" not in str(caught.value)
    assert not temp_audio.exists()


def test_temp_permission_failure_still_deletes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp_audio = _capture_tempfile(monkeypatch, tmp_path)
    monkeypatch.setattr(transcribe, "get_api_key", lambda _provider: "test-secret")
    monkeypatch.setattr(transcribe, "PlaudClient", FakePlaudClient)
    original_chmod = Path.chmod

    def fail_chmod(path: Path, mode: int) -> None:
        if path == temp_audio:
            raise OSError("simulated permission failure")
        original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", fail_chmod)

    with pytest.raises(TranscriptionError, match="temporary audio storage"):
        transcribe_file(_config(), "recording-1", confirm_upload=True)

    assert not temp_audio.exists()


def test_store_guard_prevents_duplicate_paid_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path / "community.db")
    storage.save_cmds_transcript(
        file_id="recording-1",
        model="scribe_v2",
        language="ko",
        text="existing",
        segments_json="[]",
        now=1,
    )
    monkeypatch.setattr(
        transcribe,
        "transcribe_file",
        lambda *_args, **_kwargs: pytest.fail("duplicate upload must not start"),
    )

    with pytest.raises(TranscriptionError, match="explicit force"):
        transcribe_and_store(
            _config(),
            "recording-1",
            storage=storage,
            confirm_upload=True,
        )


def test_existing_transcript_does_not_erase_unknown_newer_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path / "community.db")
    storage.save_cmds_transcript(
        file_id="recording-1",
        model="scribe_v2",
        language="ko",
        text="older successful transcript",
        segments_json="[]",
        now=1,
    )
    marker = transcribe._transcription_attempt_path(storage._db_path, "recording-1")
    transcribe._write_attempt_marker(marker, model_id="scribe_v2")
    monkeypatch.setattr(
        transcribe,
        "transcribe_file",
        lambda *_args, **_kwargs: pytest.fail("unknown retry must not upload"),
    )

    with pytest.raises(TranscriptionError, match="may bill twice"):
        transcribe_and_store(
            _config(),
            "recording-1",
            storage=storage,
            confirm_upload=True,
        )

    assert marker.is_file()


def test_base_exception_during_paid_post_preserves_unknown_attempt_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture_tempfile(monkeypatch, tmp_path)
    marker = transcribe._transcription_attempt_path(tmp_path / "community.db", "recording-1")
    monkeypatch.setattr(transcribe, "get_api_key", lambda _provider: "test-secret")
    monkeypatch.setattr(transcribe, "PlaudClient", FakePlaudClient)
    monkeypatch.setattr(transcribe, "_download_audio", lambda *_args: 10)
    monkeypatch.setattr(
        transcribe,
        "_upload_audio",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        transcribe_file(
            _config(),
            "recording-1",
            confirm_upload=True,
            attempt_path=marker,
        )

    assert marker.is_file()


def test_definite_provider_rejection_clears_attempt_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture_tempfile(monkeypatch, tmp_path)
    marker = transcribe._transcription_attempt_path(tmp_path / "community.db", "recording-1")
    monkeypatch.setattr(transcribe, "get_api_key", lambda _provider: "test-secret")
    monkeypatch.setattr(transcribe, "PlaudClient", FakePlaudClient)
    monkeypatch.setattr(transcribe, "_download_audio", lambda *_args: 10)
    monkeypatch.setattr(
        transcribe,
        "_upload_audio",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TranscriptionError("ElevenLabs transcription returned HTTP 401")
        ),
    )

    with pytest.raises(TranscriptionError, match="HTTP 401"):
        transcribe_file(
            _config(),
            "recording-1",
            confirm_upload=True,
            attempt_path=marker,
        )

    assert not marker.exists()


def test_transcription_lock_uses_private_hashed_name_and_permissions(tmp_path: Path) -> None:
    file_id = "recording-private-123"
    db_path = tmp_path / "community.db"
    expected_digest = hashlib.sha256(file_id.encode("ascii")).hexdigest()
    lock_path = transcribe._transcription_lock_path(db_path, file_id)

    assert lock_path.name == f"{expected_digest}.lock"
    assert file_id not in str(lock_path)
    with transcribe._transcription_lock(db_path, file_id):
        assert lock_path.is_file()
        if os.name == "posix":
            assert stat.S_IMODE(lock_path.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600

    # The inode intentionally persists so a third process can never bypass a
    # waiter by creating and locking a replacement file.
    assert lock_path.is_file()


def test_transcription_lock_serializes_independent_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    first_attempting = context.Event()
    first_acquired = context.Event()
    first_contended = context.Event()
    second_attempting = context.Event()
    second_acquired = context.Event()
    second_contended = context.Event()
    release = context.Event()
    db_path = str(tmp_path / "community.db")
    first = context.Process(
        target=_transcription_lock_worker,
        args=(
            db_path,
            "recording-1",
            first_attempting,
            first_acquired,
            first_contended,
            release,
            True,
        ),
    )
    second = context.Process(
        target=_transcription_lock_worker,
        args=(
            db_path,
            "recording-1",
            second_attempting,
            second_acquired,
            second_contended,
            release,
            False,
        ),
    )
    processes = (first, second)

    try:
        first.start()
        assert first_attempting.wait(timeout=10)
        assert first_acquired.wait(timeout=10)
        second.start()
        assert second_attempting.wait(timeout=10)
        assert not second_acquired.wait(timeout=0.5)
        release.set()
        assert second_acquired.wait(timeout=10)
    finally:
        release.set()
        for process in processes:
            if process.pid is None:
                continue
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert not first_contended.is_set()
    assert second_contended.is_set()


def test_transcription_lock_wait_has_bounded_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "community.db"
    lock_path = transcribe._transcription_lock_path(db_path, "recording-1")
    monkeypatch.setattr(transcribe, "_TRANSCRIPTION_LOCK_TIMEOUT_SECONDS", 0.01)

    with transcribe._same_process_lock(lock_path):
        with pytest.raises(TranscriptionError, match="remained busy"):
            with transcribe._transcription_lock(db_path, "recording-1"):
                pytest.fail("contended transcription must not acquire after its deadline")


@pytest.mark.parametrize(("force", "seed_existing"), [(False, False), (True, True)])
def test_concurrent_store_calls_only_upload_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force: bool,
    seed_existing: bool,
) -> None:
    db_path = tmp_path / "community.db"
    first_storage = Storage(db_path)
    second_storage = Storage(db_path)
    if seed_existing:
        first_storage.save_cmds_transcript(
            file_id="recording-1",
            model="scribe_v2",
            language="ko",
            text="existing transcript",
            segments_json="[]",
            now=1,
        )
    upload_started = threading.Event()
    second_upload_started = threading.Event()
    release_upload = threading.Event()
    calls_guard = threading.Lock()
    calls = 0

    def fake_transcribe(*_args, **_kwargs):
        nonlocal calls
        with calls_guard:
            calls += 1
            if calls > 1:
                second_upload_started.set()
        upload_started.set()
        assert release_upload.wait(timeout=5)
        return {
            "file_id": "recording-1",
            "provider": "elevenlabs",
            "model": "scribe_v2",
            "language": "ko",
            "text": "only one paid upload",
            "segments": [],
            "raw_words_count": 0,
            "audio_bytes_uploaded": 10,
        }

    monkeypatch.setattr(transcribe, "transcribe_file", fake_transcribe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            transcribe_and_store,
            _config(),
            "recording-1",
            storage=first_storage,
            confirm_upload=True,
            force=force,
        )
        assert upload_started.wait(timeout=2)
        second = pool.submit(
            transcribe_and_store,
            _config(),
            "recording-1",
            storage=second_storage,
            confirm_upload=True,
            force=force,
        )
        assert not second_upload_started.wait(timeout=0.5)
        release_upload.set()

        successes = []
        failures = []
        for future in (first, second):
            try:
                successes.append(future.result(timeout=5))
            except TranscriptionError as exc:
                failures.append(exc)

    assert calls == 1
    assert len(successes) == 1
    assert len(failures) == 1
    assert "already in progress" in str(failures[0])
    assert first_storage.get_cmds_transcript("recording-1")["text"] == "only one paid upload"


def test_transcription_lock_releases_after_upload_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path / "community.db")
    calls = 0

    def flaky_transcribe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TranscriptionError("simulated provider failure")
        return {
            "file_id": "recording-1",
            "provider": "elevenlabs",
            "model": "scribe_v2",
            "language": "ko",
            "text": "retry succeeded",
            "segments": [],
            "raw_words_count": 0,
            "audio_bytes_uploaded": 10,
        }

    monkeypatch.setattr(transcribe, "transcribe_file", flaky_transcribe)
    with pytest.raises(TranscriptionError, match="simulated provider failure"):
        transcribe_and_store(_config(), "recording-1", storage=storage, confirm_upload=True)

    result = transcribe_and_store(_config(), "recording-1", storage=storage, confirm_upload=True)
    assert result["text"] == "retry succeeded"
    assert calls == 2


def test_windows_locking_initializes_byte_retries_and_unlocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []
            self.failed_once = False

        def locking(self, _fd: int, mode: int, length: int) -> None:
            self.calls.append((mode, length))
            if mode == self.LK_NBLCK and not self.failed_once:
                self.failed_once = True
                raise OSError(errno.EACCES, "already locked")

    fake = FakeMsvcrt()
    monkeypatch.setattr(transcribe, "_load_msvcrt", lambda: fake)
    monkeypatch.setattr(transcribe, "_WINDOWS_LOCK_RETRY_SECONDS", 0)
    lock_path = tmp_path / "windows.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        assert transcribe._acquire_file_lock(fd, windows=True) is True
        transcribe._release_file_lock(fd, windows=True)
    finally:
        os.close(fd)

    assert lock_path.read_bytes() == b"\0"
    assert fake.calls == [
        (fake.LK_NBLCK, 1),
        (fake.LK_NBLCK, 1),
        (fake.LK_UNLCK, 1),
    ]


def test_oversized_download_is_rejected_and_temp_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp_audio = _capture_tempfile(monkeypatch, tmp_path)
    monkeypatch.setattr(transcribe, "get_api_key", lambda _provider: "test-secret")
    monkeypatch.setattr(transcribe, "PlaudClient", FakePlaudClient)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(transcribe.MAX_AUDIO_BYTES + 1)},
            request=request,
        )

    _mock_http(monkeypatch, handler)

    with pytest.raises(TranscriptionError, match="1 GiB"):
        transcribe_file(_config(), "recording-1", confirm_upload=True)

    assert not temp_audio.exists()


def test_streamed_download_cap_is_enforced_without_content_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp_audio = _capture_tempfile(monkeypatch, tmp_path)
    monkeypatch.setattr(transcribe, "MAX_AUDIO_BYTES", 4)
    monkeypatch.setattr(transcribe, "get_api_key", lambda _provider: "test-secret")
    monkeypatch.setattr(transcribe, "PlaudClient", FakePlaudClient)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"12345", request=request)

    _mock_http(monkeypatch, handler)

    with pytest.raises(TranscriptionError, match="1 GiB"):
        transcribe_file(_config(), "recording-1", confirm_upload=True)

    assert not temp_audio.exists()


def test_download_rejects_https_to_http_downgrade_before_following(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp_audio = _capture_tempfile(monkeypatch, tmp_path)
    monkeypatch.setattr(transcribe, "get_api_key", lambda _provider: "test-secret")
    monkeypatch.setattr(transcribe, "PlaudClient", FakePlaudClient)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"location": "http://insecure.example.test/audio.mp3"},
            request=request,
        )

    _mock_http(monkeypatch, handler)

    with pytest.raises(TranscriptionError, match="must be an HTTPS URL"):
        transcribe_file(_config(), "recording-1", confirm_upload=True)

    assert len(requests) == 1
    assert not temp_audio.exists()


def test_cli_refuses_upload_without_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        transcribe,
        "transcribe_and_store",
        lambda *_args, **_kwargs: pytest.fail("transcription must not start"),
    )

    result = CliRunner().invoke(app, ["elevenlabs-transcribe", "recording-1"])

    assert result.exit_code == 2
    assert "may consume paid credits" in result.output


def test_cli_attempt_status_is_local_json_and_blocks_unknown_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "community.db"

    class EmptyStorage:
        _db_path = db_path

        def get_cmds_transcript(self, _file_id: str):
            return None

    marker = transcribe._transcription_attempt_path(db_path, "recording-1")
    transcribe._write_attempt_marker(marker, model_id="scribe_v2")
    monkeypatch.setattr(cli_main, "Storage", EmptyStorage)
    monkeypatch.setattr(
        transcribe,
        "transcribe_and_store",
        lambda *_args, **_kwargs: pytest.fail("unknown retry must stop before upload"),
    )

    status = CliRunner().invoke(app, ["elevenlabs-attempt-status", "recording-1", "--json"])
    retry = CliRunner().invoke(
        app,
        ["elevenlabs-transcribe", "recording-1", "--confirm-upload", "--json"],
    )

    assert status.exit_code == 0, status.output
    assert '"status": "outcome_unknown"' in status.output
    assert '"retry_may_bill_twice": true' in status.output
    assert retry.exit_code == 2
    assert "may bill twice" in retry.output


def test_cli_json_success_is_machine_readable_without_progress_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyStorage:
        _db_path = Path("/private/tmp/unused-community-test.db")

        def get_cmds_transcript(self, _file_id: str):
            return None

    monkeypatch.setattr(cli_main, "Storage", EmptyStorage)
    monkeypatch.setattr(cli_main, "load_config", lambda: _config())
    monkeypatch.setattr(
        transcribe,
        "transcribe_and_store",
        lambda *_args, **_kwargs: {
            "file_id": "recording-1",
            "provider": "elevenlabs",
            "model": "scribe_v2",
            "language": "ko",
            "segments": [{"content": "local only"}],
            "audio_bytes_uploaded": 123,
        },
    )

    result = CliRunner().invoke(
        app,
        ["elevenlabs-transcribe", "recording-1", "--confirm-upload", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert result.output.startswith("{")
    assert "Uploading audio" not in result.output
    assert '"stored_locally": true' in result.output


def test_group_words_into_segments_splits_on_speaker_silence_and_bad_timestamps() -> None:
    words = [
        {"type": "word", "text": "안녕", "speaker_id": "speaker_0", "start": 0, "end": 0.3},
        {
            "type": "word",
            "text": "하세요",
            "speaker_id": "speaker_0",
            "start": 0.4,
            "end": 0.8,
        },
        {"type": "audio_event", "text": "laugh", "start": 0.8, "end": 0.9},
        {"type": "word", "text": "skip", "start": float("nan"), "end": 1.0},
        {"type": "word", "text": "네", "speaker_id": "speaker_1", "start": 0.9, "end": 1.1},
        {"type": "word", "text": "다시", "speaker_id": "speaker_1", "start": 3.0, "end": 3.2},
    ]

    assert transcribe.group_words_into_segments(words) == [
        {
            "speaker": "speaker_0",
            "start_ms": 0,
            "end_ms": 800,
            "content": "안녕 하세요",
        },
        {"speaker": "speaker_1", "start_ms": 900, "end_ms": 1100, "content": "네"},
        {"speaker": "speaker_1", "start_ms": 3000, "end_ms": 3200, "content": "다시"},
    ]
