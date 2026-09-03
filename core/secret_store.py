"""Atomic, OS-native Plaud credential storage for the Community edition.

The Plaud workspace refresh token rotates every time it is used.  Access and
refresh tokens therefore live in one JSON blob and are replaced together.
macOS stores that blob in Keychain. Windows encrypts it with the current user's
DPAPI key before writing ``auth.bin`` below the Community LocalAppData folder.
Non-secret application preferences remain in ``settings.env``.

Legacy ``.env`` credentials are migrated on first read.  The Keychain write is
read back and verified before any plaintext values are removed from disk.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl

    if sys.platform == "darwin":
        from keyring.backends.macOS import api as keychain_api

COMMUNITY_KEYCHAIN_SERVICE = "com.cmdspace.PlaudNoteManagerCommunity.auth"
COMMUNITY_APP_SUPPORT_ID = "com.cmdspace.PlaudNoteManagerCommunity"
WINDOWS_COMMUNITY_KEYCHAIN_SERVICE = "com.cmdspace.PlaudNoteManagerCommunity.WindowsLite.auth"
WINDOWS_COMMUNITY_APP_SUPPORT_ID = "com.cmdspace.PlaudNoteManagerCommunity.WindowsLite"
KEYCHAIN_SERVICE = os.environ.get("PLAUD_KEYCHAIN_SERVICE", COMMUNITY_KEYCHAIN_SERVICE)
KEYCHAIN_ACCOUNT = "plaud-web"
KEYCHAIN_SCHEMA_VERSION = 1
APP_SUPPORT_ID = os.environ.get("PLAUD_APP_SUPPORT_ID", COMMUNITY_APP_SUPPORT_ID)

# Keep the entire workspace-bound auth tuple together.  Splitting these values
# between Keychain and .env could pair a rotated refresh token with the wrong
# workspace/region or an older access token after a partial write.
KEYCHAIN_OWNED_KEYS = (
    "PLAUD_BASE_URL",
    "PLAUD_AUTHORIZATION",
    "PLAUD_X_DEVICE_ID",
    "PLAUD_X_PLD_USER",
    "PLAUD_X_PLD_TAG",
    "PLAUD_COOKIE",
    "PLAUD_APP_LANGUAGE",
    "PLAUD_APP_PLATFORM",
    "PLAUD_EDIT_FROM",
    "PLAUD_ORIGIN",
    "PLAUD_REFERER",
    "PLAUD_TIMEZONE",
    "PLAUD_WORKSPACE_ID",
    "PLAUD_WS_REFRESH_TOKEN",
    "PLAUD_WS_REFRESH_EXPIRES_AT",
)
_KEYCHAIN_OWNED = frozenset(KEYCHAIN_OWNED_KEYS)
_COMMUNITY_NAMESPACES = frozenset(
    {
        (COMMUNITY_KEYCHAIN_SERVICE, COMMUNITY_APP_SUPPORT_ID),
        (WINDOWS_COMMUNITY_KEYCHAIN_SERVICE, WINDOWS_COMMUNITY_APP_SUPPORT_ID),
    }
)


class CredentialStoreError(RuntimeError):
    """The native secret store could not safely handle credentials."""


def _file_backend_for_tests() -> bool:
    """Compatibility backend used only by the test suite.

    Production deliberately has no silent plaintext fallback: if the login
    native secret store is unavailable, callers receive a clear error and the
    legacy file is preserved byte-for-byte.
    """

    return os.environ.get("PLAUD_SECRET_STORE") == "test-file"


def _windows_blob_path() -> Path:
    explicit = os.environ.get("PLAUD_AUTH_BLOB_FILE")
    if explicit:
        return Path(explicit).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise CredentialStoreError("Windows LocalAppData is unavailable")
    return Path(local_app_data) / "CMDSPACE" / APP_SUPPORT_ID / "auth.bin"


if sys.platform == "win32":
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    _kernel32.LocalFree.restype = ctypes.c_void_p


def _blob_input(data: bytes):
    backing = ctypes.create_string_buffer(data)
    blob = _DataBlob(len(data), ctypes.cast(backing, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, backing


def _dpapi_protect(data: bytes) -> bytes:
    source, backing = _blob_input(data)
    protected = _DataBlob()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(source),
        "Plaud Note Manager Community credentials",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(protected),
    )
    del backing
    if not ok:
        raise CredentialStoreError(
            f"Windows DPAPI could not encrypt credentials ({ctypes.get_last_error()})"
        )
    try:
        return ctypes.string_at(protected.pbData, protected.cbData)
    finally:
        _kernel32.LocalFree(protected.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    source, backing = _blob_input(data)
    plain = _DataBlob()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(plain),
    )
    del backing
    if not ok:
        raise CredentialStoreError(
            f"Windows DPAPI could not decrypt credentials ({ctypes.get_last_error()})"
        )
    try:
        return ctypes.string_at(plain.pbData, plain.cbData)
    finally:
        _kernel32.LocalFree(plain.pbData)


def _atomic_write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp_path, flags, 0o600)
    try:
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _native_read() -> str | None:
    if sys.platform == "win32":
        path = _windows_blob_path()
        if not path.exists():
            return None
        try:
            encrypted = path.read_bytes()
            return _dpapi_unprotect(encrypted).decode("utf-8")
        except CredentialStoreError:
            raise
        except (OSError, UnicodeDecodeError) as exc:
            raise CredentialStoreError("Windows credential file could not be read") from exc
    if sys.platform != "darwin":
        raise CredentialStoreError("native credential storage is unsupported on this platform")
    try:
        return keychain_api.find_generic_password(
            None, KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, not_found_ok=True
        )
    except keychain_api.Error as exc:
        raise CredentialStoreError("macOS Keychain denied credential access") from exc


def _native_replace(payload: str) -> None:
    """Atomically replace the OS-protected credential payload."""

    if sys.platform == "win32":
        try:
            _atomic_write_private(_windows_blob_path(), _dpapi_protect(payload.encode("utf-8")))
        except CredentialStoreError:
            raise
        except OSError as exc:
            raise CredentialStoreError("Windows could not save encrypted credentials") from exc
        return
    if sys.platform != "darwin":
        raise CredentialStoreError("native credential storage is unsupported on this platform")

    sec_item_update = keychain_api._sec.SecItemUpdate
    sec_item_update.restype = keychain_api.OS_status
    sec_item_update.argtypes = (ctypes.c_void_p, ctypes.c_void_p)

    cf_data_create = keychain_api._found.CFDataCreate
    cf_data_create.restype = ctypes.c_void_p
    cf_data_create.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32)

    encoded = payload.encode("utf-8")
    buffer = ctypes.create_string_buffer(encoded)
    data = cf_data_create(None, buffer, len(encoded))
    query = keychain_api.create_query(
        kSecClass=keychain_api.k_("kSecClassGenericPassword"),
        kSecAttrService=KEYCHAIN_SERVICE,
        kSecAttrAccount=KEYCHAIN_ACCOUNT,
    )
    attributes = keychain_api.CFDictionaryCreate(
        None,
        (ctypes.c_void_p * 1)(keychain_api.k_("kSecValueData")),
        (ctypes.c_void_p * 1)(data),
        1,
        keychain_api._found.kCFTypeDictionaryKeyCallBacks,
        keychain_api._found.kCFTypeDictionaryValueCallBacks,
    )
    try:
        status = sec_item_update(query, attributes)
        if status == keychain_api.error.item_not_found:
            keychain_api.set_generic_password(None, KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, payload)
            return
        keychain_api.Error.raise_for_status(status)
    except keychain_api.Error as exc:
        raise CredentialStoreError("macOS Keychain could not save credentials") from exc


def _native_delete() -> None:
    if sys.platform == "win32":
        try:
            _windows_blob_path().unlink(missing_ok=True)
        except OSError as exc:
            raise CredentialStoreError("Windows could not remove encrypted credentials") from exc
        return
    if sys.platform != "darwin":
        raise CredentialStoreError("native credential storage is unsupported on this platform")
    try:
        keychain_api.delete_generic_password(None, KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    except keychain_api.NotFound:
        return
    except keychain_api.Error as exc:
        raise CredentialStoreError("macOS Keychain could not remove credentials") from exc


def _read_keychain() -> dict[str, str] | None:
    raw = _native_read()
    if raw is None:
        return None

    try:
        document = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CredentialStoreError("stored Plaud credentials are corrupted") from exc
    if not isinstance(document, dict) or document.get("schema_version") != KEYCHAIN_SCHEMA_VERSION:
        raise CredentialStoreError("stored Plaud credentials use an unsupported schema")
    values = document.get("values")
    if not isinstance(values, dict):
        raise CredentialStoreError("stored Plaud credentials are corrupted")

    clean: dict[str, str] = {}
    for key, value in values.items():
        if key in _KEYCHAIN_OWNED and isinstance(value, str) and value:
            clean[key] = value
    return clean


def _serialize(values: Mapping[str, str]) -> str:
    return json.dumps(
        {
            "schema_version": KEYCHAIN_SCHEMA_VERSION,
            "values": {key: values[key] for key in KEYCHAIN_OWNED_KEYS if values.get(key)},
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=False,
    )


def _write_keychain(values: Mapping[str, str]) -> None:
    expected = {key: value for key, value in values.items() if key in _KEYCHAIN_OWNED and value}
    payload = _serialize(expected)

    last_error: CredentialStoreError | None = None
    for _ in range(3):
        try:
            # Security.framework keeps the secret out of argv/stdin and
            # SecItemUpdate replaces an existing bundle in one atomic call.
            _native_replace(payload)
            if _read_keychain() == expected:
                return
            last_error = CredentialStoreError("native credential verification failed")
        except CredentialStoreError as exc:
            last_error = exc
    raise last_error or CredentialStoreError("native credential store could not save credentials")


def _delete_keychain() -> None:
    _native_delete()


def _lock_path(env_path: Path) -> Path:
    if _file_backend_for_tests():
        return env_path.with_name(env_path.name + ".lock")
    if sys.platform == "win32":
        return _windows_blob_path().with_name("auth.lock")
    return Path.home() / "Library" / "Application Support" / APP_SUPPORT_ID / "auth.lock"


def _lock_fd(fd: int) -> None:
    if sys.platform != "win32":
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
        os.fsync(fd)
    deadline = time.monotonic() + 30
    while True:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise CredentialStoreError("timed out waiting for the credential lock") from exc
            time.sleep(0.1)


def _unlock_fd(fd: int) -> None:
    if sys.platform != "win32":
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextmanager
def credential_lock(env_path: Path) -> Iterator[None]:
    """Serialize every credential mutation across the app and all checkouts."""

    lock_path = _lock_path(env_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        _lock_fd(fd)
        yield
    finally:
        _unlock_fd(fd)
        os.close(fd)


def _file_values(env_path: Path) -> dict[str, str]:
    # Lazy import avoids a config -> secret_store -> config import cycle.
    from .config import read_env_file

    return read_env_file(env_path)


def _scrub_plaintext(env_path: Path, keys: Mapping[str, object] | set[str]) -> None:
    if not env_path.exists():
        return
    from .config import update_env_file

    key_set = set(keys)
    if not key_set:
        return
    update_env_file({key: None for key in key_set}, env_path)
    for key in key_set:
        os.environ.pop(key, None)


def _load_locked(env_path: Path, *, migrate: bool) -> dict[str, str]:
    file_values = _file_values(env_path)
    legacy = {key: value for key, value in file_values.items() if key in _KEYCHAIN_OWNED and value}

    if _file_backend_for_tests():
        if env_path.exists():
            return legacy
        return {key: value for key in KEYCHAIN_OWNED_KEYS if (value := os.environ.get(key))}

    stored = _read_keychain()
    if stored is None and legacy and migrate:
        # Keychain first, verified read-back second, plaintext deletion last.
        _write_keychain(legacy)
        stored = _read_keychain()
        if stored != legacy:
            raise CredentialStoreError("legacy credential migration could not be verified")
        _scrub_plaintext(env_path, set(legacy))
    elif stored is not None and legacy and migrate:
        # Once the Keychain item exists it is authoritative.  Never replay a
        # stale .env refresh token over the newer rotated Keychain value.
        _scrub_plaintext(env_path, set(legacy))

    if stored is not None:
        return stored
    # Explicit process environment remains a supported ephemeral input; it is
    # never copied to disk or Keychain implicitly.
    return {key: value for key in KEYCHAIN_OWNED_KEYS if (value := os.environ.get(key))}


def load_credential_values(
    env_path: Path, *, migrate: bool = True, already_locked: bool = False
) -> dict[str, str]:
    if already_locked:
        return _load_locked(env_path, migrate=migrate)
    with credential_lock(env_path):
        return _load_locked(env_path, migrate=migrate)


def _update_locked(updates: Mapping[str, str | None], env_path: Path) -> None:
    if _file_backend_for_tests():
        from .config import update_env_file

        update_env_file(updates, env_path)
        return

    current = _read_keychain() or {}
    keychain_updates = {key: value for key, value in updates.items() if key in _KEYCHAIN_OWNED}
    file_updates = {key: value for key, value in updates.items() if key not in _KEYCHAIN_OWNED}
    for key, value in keychain_updates.items():
        if value:
            current[key] = value
        else:
            current.pop(key, None)

    if current:
        _write_keychain(current)
    else:
        _delete_keychain()

    # The Keychain commit and verification have succeeded.  It is now safe to
    # remove every credential key from the legacy file and current process.
    if env_path.exists() or file_updates:
        from .config import update_env_file

        scrub = {key: None for key in KEYCHAIN_OWNED_KEYS}
        update_env_file({**file_updates, **scrub}, env_path)
    for key in KEYCHAIN_OWNED_KEYS:
        os.environ.pop(key, None)


def update_credential_values(
    updates: Mapping[str, str | None], env_path: Path, *, already_locked: bool = False
) -> None:
    """Atomically merge auth updates into Keychain and non-auth updates into .env."""

    if already_locked:
        _update_locked(updates, env_path)
    else:
        with credential_lock(env_path):
            _update_locked(updates, env_path)

    # Writing a new authorization retires whatever the server rejected before.
    # Doing it here — the one choke point every credential path funnels
    # through (ws-refresh, web-auth, refresh-auth) — means no future write
    # path can forget to clear the memo and leave a healthy token marked dead.
    if updates.get("PLAUD_AUTHORIZATION"):
        from .auth_status import clear_auth_rejection

        clear_auth_rejection()


def disconnect_community_credentials(env_path: Path) -> None:
    """Remove only the Community edition's Plaud credentials.

    The namespace check prevents this public build from being pointed at the
    private application's Keychain item through environment overrides. The
    machine-managed settings file keeps every non-credential preference, and
    recordings, transcripts, caches, and the SQLite database are untouched.
    """

    if (KEYCHAIN_SERVICE, APP_SUPPORT_ID) not in _COMMUNITY_NAMESPACES:
        raise CredentialStoreError("disconnect refused outside the Community credential namespace")

    with credential_lock(env_path):
        if not _file_backend_for_tests():
            # Delete directly instead of decoding the item first so a corrupt
            # credential blob can still be disconnected safely.
            _delete_keychain()
        if env_path.exists():
            from .config import update_env_file

            update_env_file({key: None for key in KEYCHAIN_OWNED_KEYS}, env_path)

    # Environment credentials are ephemeral but would otherwise keep the
    # current process authenticated after the persistent stores were cleared.
    for key in KEYCHAIN_OWNED_KEYS:
        os.environ.pop(key, None)
