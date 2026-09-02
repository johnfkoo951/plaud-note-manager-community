"""Atomic Plaud credential storage backed by the macOS Keychain.

The Plaud workspace refresh token rotates every time it is used.  Access and
refresh tokens therefore live in one generic-password item and are replaced as
one JSON blob.  Non-secret application preferences remain in ``.env``.

Legacy ``.env`` credentials are migrated on first read.  The Keychain write is
read back and verified before any plaintext values are removed from disk.
"""

from __future__ import annotations

import fcntl
import ctypes
import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from keyring.backends.macOS import api as keychain_api

COMMUNITY_KEYCHAIN_SERVICE = "com.cmdspace.PlaudNoteManagerCommunity.auth"
COMMUNITY_APP_SUPPORT_ID = "com.cmdspace.PlaudNoteManagerCommunity"
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


class CredentialStoreError(RuntimeError):
    """The Keychain could not safely read, write, or verify credentials."""


def _file_backend_for_tests() -> bool:
    """Compatibility backend used only by the test suite.

    Production deliberately has no silent plaintext fallback: if the login
    Keychain is locked or unavailable, callers receive a clear error and the
    legacy file is preserved byte-for-byte.
    """

    return os.environ.get("PLAUD_SECRET_STORE") == "test-file"


def _native_read() -> str | None:
    try:
        return keychain_api.find_generic_password(
            None, KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, not_found_ok=True
        )
    except keychain_api.Error as exc:
        raise CredentialStoreError("macOS Keychain denied credential access") from exc


def _native_replace(payload: str) -> None:
    """Add or atomically update the generic-password data via Security.framework."""

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
            last_error = CredentialStoreError("macOS Keychain credential verification failed")
        except CredentialStoreError as exc:
            last_error = exc
    raise last_error or CredentialStoreError("macOS Keychain could not save credentials")


def _delete_keychain() -> None:
    _native_delete()


def _lock_path(env_path: Path) -> Path:
    if _file_backend_for_tests():
        return env_path.with_name(env_path.name + ".lock")
    return Path.home() / "Library" / "Application Support" / APP_SUPPORT_ID / "auth.lock"


@contextmanager
def credential_lock(env_path: Path) -> Iterator[None]:
    """Serialize every credential mutation across the app and all checkouts."""

    lock_path = _lock_path(env_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
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

    if KEYCHAIN_SERVICE != COMMUNITY_KEYCHAIN_SERVICE or APP_SUPPORT_ID != COMMUNITY_APP_SUPPORT_ID:
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
