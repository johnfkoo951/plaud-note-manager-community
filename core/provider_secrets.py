"""OS-protected API keys for opt-in Community integrations.

Provider keys deliberately do not share Plaud's credential item.  Each key has
its own fixed Keychain account on macOS or DPAPI-encrypted blob on Windows, so
rotating/deleting an integration cannot damage the Plaud authentication tuple.

Only :func:`get_api_key` returns secret material, and it is intended for the
provider request layer.  Status/reporting APIs expose only set/unset state: not
even a masked prefix or suffix is returned.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import secret_store
from .secret_store import CredentialStoreError

SUPPORTED_API_KEY_PROVIDERS = frozenset(
    {
        "anthropic",
        "elevenlabs",
        "gemini",
        "grok",
        "openai",
    }
)

_SCHEMA_VERSION = 1
_MAX_API_KEY_CHARS = 4096
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_LOCK_DIRECTORY = ".provider-secret-locks"
_WINDOWS_LOCK_RETRY_SECONDS = 0.1
_WINDOWS_LOCK_TIMEOUT_SECONDS = 30.0
_WINDOWS_LOCK_CONTENTION = frozenset(
    {None, errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLK", errno.EACCES)}
)


class _LocalLockEntry:
    """Reference-counted same-process companion to the advisory file lock."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0


_LOCAL_LOCKS_GUARD = threading.Lock()
_LOCAL_LOCKS: dict[str, _LocalLockEntry] = {}


@dataclass(frozen=True)
class _SecretTarget:
    service: str
    account: str
    windows_path: Path
    lock_path: Path


def _normalize_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    if not _PROVIDER_RE.fullmatch(normalized) or normalized not in SUPPORTED_API_KEY_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_API_KEY_PROVIDERS))
        raise ValueError(f"unsupported API-key provider; choose one of: {supported}")
    return normalized


def _target(provider: str) -> _SecretTarget:
    provider = _normalize_provider(provider)
    if sys.platform == "win32":
        service_root = secret_store.WINDOWS_COMMUNITY_KEYCHAIN_SERVICE
        app_support_id = secret_store.WINDOWS_COMMUNITY_APP_SUPPORT_ID
    else:
        service_root = secret_store.COMMUNITY_KEYCHAIN_SERVICE
        app_support_id = secret_store.COMMUNITY_APP_SUPPORT_ID

    local_app_data = os.environ.get("LOCALAPPDATA")
    auth_blob = os.environ.get("PLAUD_AUTH_BLOB_FILE")
    if sys.platform == "win32" and not (auth_blob or local_app_data):
        raise CredentialStoreError("Windows protected-storage path is unavailable")
    # This placeholder is ignored outside Windows; it avoids consulting a user
    # directory while constructing a macOS Keychain target.
    if auth_blob:
        # The Windows launcher owns the canonical Community directory. Keeping
        # provider blobs next to its auth.bin means the documented one-folder
        # uninstall removes every DPAPI ciphertext as promised.
        windows_secret_root = Path(auth_blob).expanduser().parent / "provider-secrets"
    else:
        windows_root = Path(local_app_data) if local_app_data else Path(".")
        windows_secret_root = windows_root / "CMDSPACE" / app_support_id / "provider-secrets"
    service = f"{service_root}.providers"
    account = f"api-key:{provider}"
    # The opaque name deliberately contains neither the provider name nor any
    # key material. It is stable across processes that address the same native
    # credential item.
    lock_name = hashlib.sha256(f"{service}\0{account}".encode()).hexdigest() + ".lock"
    test_lock_root = None
    if secret_store._file_backend_for_tests():
        configured_test_root = os.environ.get("PLAUD_PROVIDER_LOCK_DIRECTORY")
        if configured_test_root:
            test_lock_root = Path(configured_test_root).expanduser()
    if test_lock_root is not None:
        lock_root = test_lock_root
    elif sys.platform == "win32":
        lock_root = windows_secret_root / _LOCK_DIRECTORY
    else:
        lock_root = (
            Path.home() / "Library" / "Application Support" / app_support_id / _LOCK_DIRECTORY
        )
    return _SecretTarget(
        service=service,
        account=account,
        windows_path=windows_secret_root / f"{provider}.bin",
        lock_path=lock_root / lock_name,
    )


def _load_msvcrt() -> Any:
    import msvcrt

    return msvcrt


def _load_fcntl() -> Any:
    import fcntl

    return fcntl


def _acquire_file_lock(fd: int, *, windows: bool | None = None) -> None:
    use_windows = sys.platform == "win32" if windows is None else windows
    if not use_windows:
        module = _load_fcntl()
        module.flock(fd, module.LOCK_EX)
        return

    module = _load_msvcrt()
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
        os.fsync(fd)
    deadline = time.monotonic() + _WINDOWS_LOCK_TIMEOUT_SECONDS
    while True:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            module.locking(fd, module.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in _WINDOWS_LOCK_CONTENTION:
                raise
            if time.monotonic() >= deadline:
                raise CredentialStoreError(
                    "timed out waiting for the provider credential lock"
                ) from exc
            time.sleep(_WINDOWS_LOCK_RETRY_SECONDS)


def _release_file_lock(fd: int, *, windows: bool | None = None) -> None:
    use_windows = sys.platform == "win32" if windows is None else windows
    if not use_windows:
        module = _load_fcntl()
        module.flock(fd, module.LOCK_UN)
        return
    module = _load_msvcrt()
    os.lseek(fd, 0, os.SEEK_SET)
    module.locking(fd, module.LK_UNLCK, 1)


@contextmanager
def _same_process_lock(path: Path) -> Iterator[None]:
    """Prevent threads in this process from bypassing process-scoped locks."""

    key = os.path.normcase(os.path.abspath(path))
    with _LOCAL_LOCKS_GUARD:
        entry = _LOCAL_LOCKS.setdefault(key, _LocalLockEntry())
        entry.users += 1

    acquired = False
    try:
        entry.lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            entry.lock.release()
        with _LOCAL_LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0 and _LOCAL_LOCKS.get(key) is entry:
                del _LOCAL_LOCKS[key]


def _prepare_private_lock_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "posix":
        return
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError(errno.ENOTDIR, "provider lock directory is not a directory")
    path.chmod(0o700)


@contextmanager
def _provider_secret_lock(provider: str) -> Iterator[_SecretTarget]:
    """Exclusively lock one provider item across threads and processes."""

    provider = _normalize_provider(provider)
    target = _target(provider)
    with _same_process_lock(target.lock_path):
        fd: int | None = None
        acquired = False
        try:
            _prepare_private_lock_directory(target.lock_path.parent)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target.lock_path, flags, 0o600)
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(errno.EINVAL, "provider lock is not a regular file")
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            _acquire_file_lock(fd)
            acquired = True
        except CredentialStoreError:
            if fd is not None:
                os.close(fd)
            raise
        except (ImportError, OSError) as exc:
            if fd is not None:
                os.close(fd)
            raise CredentialStoreError("private provider credential lock is unavailable") from exc
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise

        try:
            yield target
        finally:
            # Never unlink the persistent lock. Replacing its inode could let a
            # third process enter while an earlier process still holds the old
            # inode. Closing is the final release fallback on both platforms.
            if acquired and fd is not None:
                try:
                    _release_file_lock(fd)
                except (ImportError, OSError):
                    pass
                finally:
                    os.close(fd)


def _validate_key(api_key: str) -> str:
    # A line-oriented stdin command is the only supported UI boundary. Strip
    # terminal newlines but reject embedded whitespace/control characters that
    # usually signal a pasted label or multiple secrets.
    clean = api_key.strip()
    if not clean:
        raise ValueError("API key is empty")
    if len(clean) > _MAX_API_KEY_CHARS:
        raise ValueError("API key is too long")
    if not clean.isascii() or any(not 0x21 <= ord(char) <= 0x7E for char in clean):
        raise ValueError("API key must be one printable ASCII value")
    return clean


def _serialize(provider: str, api_key: str) -> str:
    return json.dumps(
        {
            "schema_version": _SCHEMA_VERSION,
            "provider": provider,
            "api_key": api_key,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _read_payload(target: _SecretTarget) -> str | None:
    return secret_store._native_read(
        service=target.service,
        account=target.account,
        windows_path=target.windows_path,
    )


def _decode(provider: str, raw: str) -> str:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CredentialStoreError(f"stored {provider} API key is corrupted") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _SCHEMA_VERSION
        or payload.get("provider") != provider
        or not isinstance(payload.get("api_key"), str)
    ):
        raise CredentialStoreError(f"stored {provider} API key uses an unsupported schema")
    try:
        return _validate_key(payload["api_key"])
    except ValueError as exc:
        raise CredentialStoreError(f"stored {provider} API key is corrupted") from exc


def set_api_key(provider: str, api_key: str) -> None:
    """Atomically store and verify one provider API key."""

    provider = _normalize_provider(provider)
    clean = _validate_key(api_key)
    payload = _serialize(provider, clean)
    with _provider_secret_lock(provider) as target:
        secret_store._native_replace(
            payload,
            service=target.service,
            account=target.account,
            windows_path=target.windows_path,
        )
        stored = _read_payload(target)
        if stored is None:
            raise CredentialStoreError(f"{provider} API key verification failed")
        verified = _decode(provider, stored)
        if not hmac.compare_digest(verified, clean):
            raise CredentialStoreError(f"{provider} API key verification failed")


def get_api_key(provider: str) -> str | None:
    """Return a provider key for an internal API call; never use in UI output."""

    provider = _normalize_provider(provider)
    with _provider_secret_lock(provider) as target:
        raw = _read_payload(target)
        return None if raw is None else _decode(provider, raw)


def has_api_key(provider: str) -> bool:
    return get_api_key(provider) is not None


def api_key_status(provider: str) -> dict[str, str | bool]:
    """Return disclosure-free status (no masked fragment or fingerprint)."""

    provider = _normalize_provider(provider)
    configured = has_api_key(provider)
    return {
        "provider": provider,
        "configured": configured,
        "status": "set" if configured else "unset",
        "secret_disclosed": False,
    }


def delete_api_key(provider: str) -> bool:
    """Delete one provider item, leaving Plaud and other providers untouched."""

    provider = _normalize_provider(provider)
    with _provider_secret_lock(provider) as target:
        try:
            existed = _read_payload(target) is not None
        except CredentialStoreError:
            # An explicit delete must also recover from an unreadable/corrupted
            # DPAPI blob or Keychain payload. The native delete remains scoped
            # to this one provider item.
            existed = True
        secret_store._native_delete(
            service=target.service,
            account=target.account,
            windows_path=target.windows_path,
        )
        if _read_payload(target) is not None:
            raise CredentialStoreError(f"{provider} API key deletion could not be verified")
        return existed
