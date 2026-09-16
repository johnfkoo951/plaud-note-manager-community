"""Opt-in ElevenLabs batch transcription for Community recordings.

The boundary is intentionally explicit: Plaud supplies a short-lived audio
URL, the audio is downloaded to a private temporary file, and that file is
uploaded to ElevenLabs only after the caller confirms the disclosure/cost.
The temporary file is removed on success, provider failure, and cancellation.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urljoin, urlsplit

import httpx

from .client import PlaudClient
from .config import PlaudConfig
from .provider_secrets import get_api_key
from .storage import Storage

ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
DEFAULT_ELEVENLABS_MODEL = "scribe_v2"

# ElevenLabs currently accepts files below 5 GB. Community deliberately keeps
# a much smaller local ceiling to bound disk use in workshops and portable
# builds while still accommodating long, compressed Plaud recordings.
MAX_AUDIO_BYTES = 1024 * 1024 * 1024
MAX_REDIRECTS = 5

_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=20.0, read=180.0, write=60.0, pool=20.0)
_UPLOAD_TIMEOUT = httpx.Timeout(connect=20.0, read=900.0, write=900.0, pool=20.0)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_LOCK_DIRECTORY = ".elevenlabs-transcription-locks"
_ATTEMPT_DIRECTORY = ".elevenlabs-transcription-attempts"
_ATTEMPT_SCHEMA_VERSION = 1
_WINDOWS_LOCK_RETRY_SECONDS = 0.1
_TRANSCRIPTION_LOCK_TIMEOUT_SECONDS = 30.0
_TEMP_AUDIO_DIRECTORY = "plaud-note-manager-community-audio"
# Longer than the bounded 15-minute provider upload plus download window. A
# hard-killed process is scavenged only by a later transcription after this
# threshold, so an active attempt can never be mistaken for stale audio.
_TEMP_AUDIO_STALE_SECONDS = 6 * 60 * 60
_WINDOWS_LOCK_CONTENTION = frozenset(
    {None, errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLK", errno.EACCES)}
)


class TranscriptionError(RuntimeError):
    """Safe, user-facing failure without API keys or response bodies."""


class TranscriptionOutcomeUnknown(TranscriptionError):
    """The provider may have accepted a billable upload before the failure."""


class _LocalLockEntry:
    """Reference-counted same-process companion to the OS file lock."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0


_LOCAL_LOCKS_GUARD = threading.Lock()
_LOCAL_LOCKS: dict[str, _LocalLockEntry] = {}


def _validated_file_id(file_id: str) -> str:
    if not _FILE_ID_RE.fullmatch(file_id):
        raise ValueError("invalid Plaud file id")
    return file_id


def _transcription_lock_path(db_path: Path, file_id: str) -> Path:
    """Return a private path that never exposes the Plaud file id."""

    digest = hashlib.sha256(file_id.encode("ascii")).hexdigest()
    return db_path.parent / _LOCK_DIRECTORY / f"{digest}.lock"


def _transcription_attempt_path(db_path: Path, file_id: str) -> Path:
    """Return an opaque private marker for a possibly billable prior upload."""

    digest = hashlib.sha256(_validated_file_id(file_id).encode("ascii")).hexdigest()
    return db_path.parent / _ATTEMPT_DIRECTORY / f"{digest}.json"


def transcription_retry_outcome_unknown(db_path: Path, file_id: str) -> bool:
    """Inspect only local durable state; never contact Plaud or ElevenLabs."""

    path = _transcription_attempt_path(Path(db_path), file_id)
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _is_windows_runtime() -> bool:
    return os.name == "nt"


def _move_file_windows(
    source: Path,
    destination: Path,
    *,
    replace_existing: bool,
) -> None:
    """Rename with Windows write-through semantics and no shell involvement."""

    import ctypes
    from ctypes import wintypes

    move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move_file.restype = wintypes.BOOL
    flags = 0x00000008  # MOVEFILE_WRITE_THROUGH
    if replace_existing:
        flags |= 0x00000001  # MOVEFILE_REPLACE_EXISTING
    if not move_file(str(source), str(destination), flags):
        raise ctypes.WinError(ctypes.get_last_error())


def _fsync_directory(path: Path) -> None:
    """Persist a POSIX directory entry change; Windows uses write-through rename."""

    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_attempt_marker(path: Path, *, model_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        directory_metadata = path.parent.lstat()
        if not stat.S_ISDIR(directory_metadata.st_mode):
            raise OSError(errno.ENOTDIR, "transcription attempt path is not a directory")
        path.parent.chmod(0o700)
    payload = json.dumps(
        {
            "schema_version": _ATTEMPT_SCHEMA_VERSION,
            "state": "outcome-unknown",
            "attempted_at": int(time.time()),
            "model": model_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError(errno.EIO, "transcription attempt marker write failed")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if _is_windows_runtime():
            _move_file_windows(temporary, path, replace_existing=True)
        else:
            os.replace(temporary, path)
        if not _is_windows_runtime() and os.name == "posix":
            path.chmod(0o600)
            _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _clear_attempt_marker(path: Path) -> None:
    if _is_windows_runtime():
        # Rename is the durable state transition. If deletion is interrupted,
        # only an opaque tombstone remains and the live marker cannot reappear.
        tombstone = path.with_name(f".{path.name}.{secrets.token_hex(8)}.deleted")
        try:
            _move_file_windows(path, tombstone, replace_existing=False)
        except FileNotFoundError:
            return
        try:
            tombstone.unlink(missing_ok=True)
        except OSError:
            pass
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _load_msvcrt() -> Any:
    import msvcrt

    return msvcrt


def _load_fcntl() -> Any:
    import fcntl

    return fcntl


def _acquire_file_lock(
    fd: int,
    *,
    windows: bool | None = None,
    deadline: float | None = None,
) -> bool:
    """Acquire the lock and return whether another owner made us wait."""

    use_windows = os.name == "nt" if windows is None else windows
    if not use_windows:
        module = _load_fcntl()
        contended = False
        while True:
            try:
                module.flock(fd, module.LOCK_EX | module.LOCK_NB)
                return contended
            except BlockingIOError:
                contended = True
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("file lock acquisition timed out") from None
                time.sleep(_WINDOWS_LOCK_RETRY_SECONDS)

    module = _load_msvcrt()
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
        os.fsync(fd)
    contended = False
    while True:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            module.locking(fd, module.LK_NBLCK, 1)
            return contended
        except OSError as exc:
            if exc.errno not in _WINDOWS_LOCK_CONTENTION:
                raise
            contended = True
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("file lock acquisition timed out") from None
            time.sleep(_WINDOWS_LOCK_RETRY_SECONDS)


def _release_file_lock(fd: int, *, windows: bool | None = None) -> None:
    use_windows = os.name == "nt" if windows is None else windows
    if not use_windows:
        module = _load_fcntl()
        module.flock(fd, module.LOCK_UN)
        return
    module = _load_msvcrt()
    os.lseek(fd, 0, os.SEEK_SET)
    module.locking(fd, module.LK_UNLCK, 1)


@contextmanager
def _same_process_lock(path: Path, *, deadline: float | None = None) -> Iterator[bool]:
    """Make semantics consistent where an OS lock is process-scoped."""

    key = os.path.normcase(os.path.abspath(path))
    with _LOCAL_LOCKS_GUARD:
        entry = _LOCAL_LOCKS.setdefault(key, _LocalLockEntry())
        entry.users += 1

    acquired = False
    contended = False
    try:
        acquired = entry.lock.acquire(blocking=False)
        if not acquired:
            contended = True
            if deadline is None:
                entry.lock.acquire()
                acquired = True
            else:
                acquired = entry.lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
                if not acquired:
                    raise TimeoutError("same-process lock acquisition timed out")
        yield contended
    finally:
        if acquired:
            entry.lock.release()
        with _LOCAL_LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0 and _LOCAL_LOCKS.get(key) is entry:
                del _LOCAL_LOCKS[key]


@contextmanager
def _transcription_lock(db_path: Path, file_id: str) -> Iterator[bool]:
    """Serialize one billable transcription across threads and processes."""

    lock_path = _transcription_lock_path(db_path, _validated_file_id(file_id))
    deadline = time.monotonic() + _TRANSCRIPTION_LOCK_TIMEOUT_SECONDS
    try:
        local_lock = _same_process_lock(lock_path, deadline=deadline)
        local_context = local_lock.__enter__()
    except TimeoutError:
        raise TranscriptionError(
            "another transcription remained busy; review or restart it before retrying"
        ) from None
    try:
        local_contended = local_context
        fd: int | None = None
        acquired = False
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == "posix":
                lock_path.parent.chmod(0o700)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(lock_path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "transcription lock is not a regular file")
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            os_contended = _acquire_file_lock(fd, deadline=deadline)
            contended = local_contended or os_contended
            acquired = True
        except TimeoutError:
            if fd is not None:
                os.close(fd)
            raise TranscriptionError(
                "another transcription remained busy; review or restart it before retrying"
            ) from None
        except (ImportError, OSError):
            if fd is not None:
                os.close(fd)
            raise TranscriptionError("private transcription lock is unavailable") from None
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise

        try:
            yield contended
        finally:
            # Closing an fd releases both flock and LockFile-style locks even if
            # an explicit unlock unexpectedly fails. Never unlink the persistent
            # lock file: doing so could let a third process lock a different inode.
            if acquired and fd is not None:
                try:
                    _release_file_lock(fd)
                except (ImportError, OSError):
                    pass
                finally:
                    os.close(fd)
    finally:
        local_lock.__exit__(None, None, None)


def _validate_https_url(value: str, *, label: str) -> str:
    if len(value) > 8192:
        raise TranscriptionError(f"{label} URL is too long")
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise TranscriptionError(f"{label} must be an HTTPS URL without embedded credentials")
    return value


def _validated_model(model_id: str) -> str:
    model = model_id.strip()
    if not model or len(model) > 64:
        raise ValueError("ElevenLabs model id must contain 1-64 characters")
    if not all(char.isalnum() or char in "_-" for char in model):
        raise ValueError("ElevenLabs model id contains unsupported characters")
    return model


def _validated_language(language_code: str | None) -> str | None:
    if language_code is None or not language_code.strip():
        return None
    language = language_code.strip().lower()
    if len(language) not in (2, 3) or not language.isalpha() or not language.isascii():
        raise ValueError("language code must be an ISO-639-1 or ISO-639-3 code")
    return language


def _validated_speakers(num_speakers: int | None) -> int | None:
    if num_speakers is None or num_speakers == 0:
        return None
    if not 1 <= num_speakers <= 32:
        raise ValueError("num_speakers must be between 1 and 32")
    return num_speakers


def _temporary_audio_directory() -> Path:
    return Path(tempfile.gettempdir()) / _TEMP_AUDIO_DIRECTORY


def _prepare_private_temp_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError(errno.ENOTDIR, "temporary audio path is not a directory")
    if os.name == "posix":
        path.chmod(0o700)


def _scavenge_stale_temp_audio(directory: Path, *, now: float | None = None) -> int:
    """Remove only this app's old regular audio files while its lock is held."""

    cutoff = (time.time() if now is None else now) - _TEMP_AUDIO_STALE_SECONDS
    removed = 0
    for candidate in directory.iterdir():
        if not candidate.name.startswith("plaud-community-"):
            continue
        if candidate.suffix.casefold() not in {".mp3", ".opus"}:
            continue
        try:
            metadata = candidate.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mtime > cutoff:
                continue
            candidate.unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            # Cleanup is best-effort. Never broaden the match or follow links
            # merely because one stale artifact cannot be removed.
            continue
    return removed


@contextmanager
def _temp_audio_directory_lock(directory: Path) -> Iterator[None]:
    """Serialize cleanup and active temp use across app/CLI processes."""

    lock_path = directory / ".cleanup.lock"
    deadline = time.monotonic() + _TRANSCRIPTION_LOCK_TIMEOUT_SECONDS
    try:
        with _same_process_lock(lock_path, deadline=deadline):
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(lock_path, flags, 0o600)
            acquired = False
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError(errno.EINVAL, "temporary audio lock is not a regular file")
                if os.name == "posix":
                    os.fchmod(fd, 0o600)
                _acquire_file_lock(fd, deadline=deadline)
                acquired = True
                yield
            finally:
                if acquired:
                    try:
                        _release_file_lock(fd)
                    except (ImportError, OSError):
                        pass
                os.close(fd)
    except TimeoutError:
        raise TranscriptionError(
            "temporary audio storage remained busy; retry after the other transcription finishes"
        ) from None
    except (ImportError, OSError):
        raise TranscriptionError("private temporary audio storage is unavailable") from None


@contextmanager
def _temporary_audio(*, suffix: str) -> Iterator[Path]:
    if suffix not in {".mp3", ".opus"}:
        raise TranscriptionError("Plaud returned an unsupported audio format")
    directory = _temporary_audio_directory()
    try:
        _prepare_private_temp_directory(directory)
        with _temp_audio_directory_lock(directory):
            _scavenge_stale_temp_audio(directory)
            fd, raw_path = tempfile.mkstemp(
                dir=directory,
                prefix="plaud-community-",
                suffix=suffix,
            )
            os.close(fd)
            path = Path(raw_path)
            try:
                if os.name == "posix":
                    path.chmod(0o600)
                yield path
            finally:
                path.unlink(missing_ok=True)
    except TranscriptionError:
        raise
    except OSError:
        raise TranscriptionError("private temporary audio storage is unavailable") from None


def _download_audio(url: str, target: Path) -> int:
    """Download through HTTPS-only redirects while enforcing a hard byte cap."""

    current = _validate_https_url(url, label="Plaud audio")
    try:
        with httpx.Client(
            timeout=_DOWNLOAD_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                with client.stream("GET", current) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise TranscriptionError("Plaud audio redirect omitted its destination")
                        current = _validate_https_url(
                            urljoin(current, location), label="Plaud audio redirect"
                        )
                        continue

                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared:
                        try:
                            declared_bytes = int(declared)
                        except ValueError as exc:
                            raise TranscriptionError(
                                "Plaud audio returned an invalid Content-Length"
                            ) from exc
                        if declared_bytes < 0 or declared_bytes > MAX_AUDIO_BYTES:
                            raise TranscriptionError(
                                "Plaud audio exceeds the Community 1 GiB upload limit"
                            )

                    downloaded = 0
                    with target.open("wb") as output:
                        for chunk in response.iter_bytes():
                            downloaded += len(chunk)
                            if downloaded > MAX_AUDIO_BYTES:
                                raise TranscriptionError(
                                    "Plaud audio exceeds the Community 1 GiB upload limit"
                                )
                            output.write(chunk)
                    if downloaded == 0:
                        raise TranscriptionError("Plaud returned an empty audio file")
                    return downloaded
    except TranscriptionError:
        raise
    except httpx.HTTPStatusError as exc:
        raise TranscriptionError(
            f"Plaud audio download returned HTTP {exc.response.status_code}"
        ) from None
    except httpx.RequestError as exc:
        raise TranscriptionError(
            f"Plaud audio download failed ({exc.__class__.__name__})"
        ) from None
    except OSError:
        raise TranscriptionError("temporary audio could not be written") from None
    raise TranscriptionError("Plaud audio exceeded the HTTPS redirect limit")


def _upload_audio(
    path: Path,
    *,
    api_key: str,
    upload_filename: str,
    content_type: str,
    model_id: str,
    diarize: bool,
    language_code: str | None,
    num_speakers: int | None,
) -> dict[str, Any]:
    # Intentionally one attempt: retrying a timed-out POST could create a
    # second billable transcription after the provider accepted the first.
    fields = {
        "model_id": model_id,
        "diarize": "true" if diarize else "false",
        "timestamps_granularity": "word",
        "tag_audio_events": "false",
    }
    if language_code:
        fields["language_code"] = language_code
    if num_speakers:
        fields["num_speakers"] = str(num_speakers)

    try:
        with httpx.Client(
            timeout=_UPLOAD_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with path.open("rb") as audio:
                response = client.post(
                    ELEVENLABS_STT_URL,
                    headers={"xi-api-key": api_key},
                    files={"file": (upload_filename, audio, content_type)},
                    data=fields,
                )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        error_type = (
            TranscriptionOutcomeUnknown if status == 408 or status >= 500 else TranscriptionError
        )
        raise error_type(f"ElevenLabs transcription returned HTTP {status}") from None
    except httpx.RequestError as exc:
        raise TranscriptionOutcomeUnknown(
            "ElevenLabs upload outcome is unknown after " + exc.__class__.__name__
        ) from None
    except OSError:
        raise TranscriptionOutcomeUnknown(
            "ElevenLabs upload outcome is unknown after a local stream error"
        ) from None

    try:
        payload = response.json()
    except ValueError:
        raise TranscriptionOutcomeUnknown(
            "ElevenLabs accepted the upload but returned a non-JSON transcription"
        ) from None
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise TranscriptionOutcomeUnknown(
            "ElevenLabs accepted the upload but returned an unsupported transcription response"
        )
    words = payload.get("words")
    if words is not None and not isinstance(words, list):
        raise TranscriptionOutcomeUnknown(
            "ElevenLabs accepted the upload but returned unsupported word timestamps"
        )
    return payload


def transcribe_file(
    cfg: PlaudConfig,
    file_id: str,
    *,
    confirm_upload: bool,
    diarize: bool = True,
    model_id: str = DEFAULT_ELEVENLABS_MODEL,
    language_code: str | None = None,
    num_speakers: int | None = None,
    attempt_path: Path | None = None,
) -> dict[str, Any]:
    """Upload one Plaud recording to ElevenLabs after explicit confirmation."""

    if not confirm_upload:
        raise TranscriptionError(
            "confirmation required: audio will be uploaded to ElevenLabs and may incur cost"
        )
    _validated_file_id(file_id)
    model = _validated_model(model_id)
    language = _validated_language(language_code)
    speakers = _validated_speakers(num_speakers)
    api_key = get_api_key("elevenlabs")
    if not api_key:
        raise TranscriptionError(
            "ElevenLabs API key is not configured; add it in Settings or via stdin"
        )

    with PlaudClient(cfg) as plaud:
        audio_source = plaud.temp_audio_source(file_id)

    try:
        suffix = Path(audio_source.filename).suffix.casefold()
        with _temporary_audio(suffix=suffix) as tmp_path:
            audio_bytes = _download_audio(audio_source.url, tmp_path)
            if attempt_path is not None:
                try:
                    _write_attempt_marker(attempt_path, model_id=model)
                except OSError:
                    raise TranscriptionError(
                        "could not durably record the paid upload attempt; no upload was sent"
                    ) from None
            try:
                payload = _upload_audio(
                    tmp_path,
                    api_key=api_key,
                    upload_filename=audio_source.filename,
                    content_type=audio_source.content_type,
                    model_id=model,
                    diarize=diarize,
                    language_code=language,
                    num_speakers=speakers,
                )
            except TranscriptionOutcomeUnknown:
                raise
            except TranscriptionError:
                if attempt_path is not None:
                    try:
                        _clear_attempt_marker(attempt_path)
                    except OSError:
                        pass
                raise
    except OSError:
        raise TranscriptionError("private temporary audio storage is unavailable") from None

    words = payload.get("words") or []
    segments = group_words_into_segments(words)
    return {
        "file_id": file_id,
        "provider": "elevenlabs",
        "model": model,
        "language": payload.get("language_code"),
        "language_probability": payload.get("language_probability"),
        "text": payload["text"],
        "segments": segments,
        "raw_words_count": len(words),
        "audio_bytes_uploaded": audio_bytes,
    }


def transcribe_and_store(
    cfg: PlaudConfig,
    file_id: str,
    *,
    storage: Storage,
    confirm_upload: bool,
    force: bool = False,
    diarize: bool = True,
    model_id: str = DEFAULT_ELEVENLABS_MODEL,
    language_code: str | None = None,
    num_speakers: int | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Transcribe and persist the result in the Community SQLite database."""

    _validated_file_id(file_id)
    try:
        db_path = Path(storage._db_path)
    except (AttributeError, TypeError):
        raise TranscriptionError("private transcription lock is unavailable") from None

    with _transcription_lock(db_path, file_id) as contended:
        if contended:
            raise TranscriptionError(
                "another transcription was already in progress; review its result before retrying"
            )
        existing = storage.get_cmds_transcript(file_id)
        attempt_path = _transcription_attempt_path(db_path, file_id)
        if transcription_retry_outcome_unknown(db_path, file_id) and not force:
            raise TranscriptionError(
                "a prior ElevenLabs upload outcome is unknown; retry with explicit force only "
                "after confirming it may bill twice"
            )
        if existing is not None and not force:
            raise TranscriptionError(
                "a local external transcript already exists; explicit force is required to upload again"
            )
        result = transcribe_file(
            cfg,
            file_id,
            confirm_upload=confirm_upload,
            diarize=diarize,
            model_id=model_id,
            language_code=language_code,
            num_speakers=num_speakers,
            attempt_path=attempt_path,
        )
        try:
            storage.save_cmds_transcript(
                file_id=file_id,
                model=result["model"],
                language=result.get("language"),
                text=result["text"],
                segments_json=json.dumps(result["segments"], ensure_ascii=False),
                now=int(time.time()) if now is None else now,
            )
            _clear_attempt_marker(attempt_path)
        except Exception:
            raise TranscriptionOutcomeUnknown(
                "ElevenLabs completed, but local persistence is uncertain; do not retry "
                "without confirming it may bill twice"
            ) from None
        return result


def group_words_into_segments(
    words: list[dict[str, Any]], *, max_silence_s: float = 1.5
) -> list[dict[str, Any]]:
    """Coalesce word events into speaker-bounded local transcript segments."""

    segments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for word in words:
        if not isinstance(word, dict) or word.get("type") not in (None, "word", "spacing"):
            continue
        text = str(word.get("text") or "").strip()
        if not text:
            continue
        speaker = str(word.get("speaker_id") or word.get("speaker") or "speaker_0")[:128]
        try:
            start = float(word.get("start") or 0)
            end = float(word.get("end") or start)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        start = max(0.0, start)
        end = max(start, end)

        gap = start - (current["end_ms"] / 1000.0) if current else 0.0
        if current is None or current["speaker"] != speaker or gap > max_silence_s:
            if current is not None:
                segments.append(current)
            current = {
                "speaker": speaker,
                "start_ms": int(start * 1000),
                "end_ms": int(end * 1000),
                "content": text,
            }
        else:
            current["end_ms"] = int(end * 1000)
            current["content"] = f"{current['content']} {text}".strip()

    if current is not None:
        segments.append(current)
    return segments
