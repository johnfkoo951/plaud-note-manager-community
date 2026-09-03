from __future__ import annotations

import os
from pathlib import Path

import pytest

import core.secret_store as store_mod
from core.config import read_env_file, write_env_file
from core.secret_store import (
    CredentialStoreError,
    disconnect_community_credentials,
    load_credential_values,
    update_credential_values,
)


class FakeKeychain:
    def __init__(self) -> None:
        self.stored: str | None = None
        self.replacements: list[str] = []
        self.fail_write = False

    def read(self) -> str | None:
        return self.stored

    def replace(self, payload: str) -> None:
        self.replacements.append(payload)
        if self.fail_write:
            raise CredentialStoreError("denied")
        self.stored = payload

    def delete(self) -> None:
        self.stored = None


@pytest.fixture
def fake_keychain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeKeychain:
    fake = FakeKeychain()
    monkeypatch.delenv("PLAUD_SECRET_STORE", raising=False)
    monkeypatch.setattr(store_mod, "_native_read", fake.read)
    monkeypatch.setattr(store_mod, "_native_replace", fake.replace)
    monkeypatch.setattr(store_mod, "_native_delete", fake.delete)
    monkeypatch.setattr(store_mod, "_lock_path", lambda env_path: tmp_path / "auth.lock")
    return fake


def test_keychain_bundle_round_trip_is_one_atomic_blob(
    tmp_path: Path, fake_keychain: FakeKeychain
) -> None:
    env = tmp_path / ".env"
    token = "bearer test-secret-access-token"

    update_credential_values(
        {
            "PLAUD_AUTHORIZATION": token,
            "PLAUD_X_DEVICE_ID": "device-1",
            "PLAUD_WORKSPACE_ID": "ws_abc",
            "PLAUD_WS_REFRESH_TOKEN": "test-secret-refresh-token",
            "PLAUD_AUTO_REFRESH": "1",
        },
        env,
    )

    loaded = load_credential_values(env)
    assert loaded["PLAUD_AUTHORIZATION"] == token
    assert loaded["PLAUD_WS_REFRESH_TOKEN"] == "test-secret-refresh-token"
    assert read_env_file(env) == {"PLAUD_AUTO_REFRESH": "1"}
    assert len(fake_keychain.replacements) == 1
    assert fake_keychain.stored == fake_keychain.replacements[0]


def test_legacy_env_migrates_only_after_verified_keychain_write(
    tmp_path: Path, fake_keychain: FakeKeychain
) -> None:
    env = tmp_path / ".env"
    write_env_file(
        {
            "PLAUD_AUTHORIZATION": "bearer legacy-token",
            "PLAUD_X_DEVICE_ID": "legacy-device",
            "PLAUD_WS_REFRESH_TOKEN": "legacy-refresh",
            "PLAUD_AUTO_REFRESH": "1",
        },
        env,
    )

    loaded = load_credential_values(env)

    assert loaded["PLAUD_AUTHORIZATION"] == "bearer legacy-token"
    assert loaded["PLAUD_WS_REFRESH_TOKEN"] == "legacy-refresh"
    assert read_env_file(env) == {"PLAUD_AUTO_REFRESH": "1"}
    assert fake_keychain.stored is not None


def test_failed_keychain_migration_preserves_legacy_env_byte_for_byte(
    tmp_path: Path, fake_keychain: FakeKeychain
) -> None:
    env = tmp_path / ".env"
    write_env_file(
        {
            "PLAUD_AUTHORIZATION": "bearer keep-me",
            "PLAUD_X_DEVICE_ID": "keep-device",
        },
        env,
    )
    before = env.read_bytes()
    fake_keychain.fail_write = True

    with pytest.raises(CredentialStoreError):
        load_credential_values(env)

    assert env.read_bytes() == before


def test_existing_keychain_is_authoritative_over_stale_legacy_env(
    tmp_path: Path, fake_keychain: FakeKeychain
) -> None:
    env = tmp_path / ".env"
    fake_keychain.stored = store_mod._serialize(
        {
            "PLAUD_AUTHORIZATION": "bearer rotated-token",
            "PLAUD_X_DEVICE_ID": "device-new",
            "PLAUD_WS_REFRESH_TOKEN": "refresh-new",
        }
    )
    write_env_file(
        {
            "PLAUD_AUTHORIZATION": "bearer stale-token",
            "PLAUD_X_DEVICE_ID": "device-old",
            "PLAUD_WS_REFRESH_TOKEN": "refresh-old",
            "PLAUD_AUTO_REFRESH": "1",
        },
        env,
    )

    loaded = load_credential_values(env)

    assert loaded["PLAUD_AUTHORIZATION"] == "bearer rotated-token"
    assert loaded["PLAUD_WS_REFRESH_TOKEN"] == "refresh-new"
    assert read_env_file(env) == {"PLAUD_AUTO_REFRESH": "1"}


def test_community_disconnect_removes_credentials_only(
    tmp_path: Path, fake_keychain: FakeKeychain, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_env = tmp_path / "settings.env"
    database = tmp_path / "data" / "plaud.db"
    transcript = tmp_path / "data" / "transcripts" / "recording-1" / "plaud.md"
    database.parent.mkdir(parents=True)
    transcript.parent.mkdir(parents=True)
    database.write_bytes(b"database-sentinel")
    transcript.write_text("transcript-sentinel", encoding="utf-8")
    fake_keychain.stored = store_mod._serialize(
        {
            "PLAUD_AUTHORIZATION": "credential-placeholder",
            "PLAUD_X_DEVICE_ID": "device-placeholder",
        }
    )
    write_env_file(
        {
            "PLAUD_AUTHORIZATION": "legacy-placeholder",
            "PLAUD_COOKIE": "cookie-placeholder",
            "PLAUD_AUTO_REFRESH": "1",
            "PLAUD_AUTHOR": "Workshop User",
        },
        settings_env,
    )
    monkeypatch.setenv("PLAUD_AUTHORIZATION", "process-placeholder")
    monkeypatch.setenv("PLAUD_COOKIE", "process-cookie-placeholder")

    disconnect_community_credentials(settings_env)

    assert fake_keychain.stored is None
    assert read_env_file(settings_env) == {
        "PLAUD_AUTO_REFRESH": "1",
        "PLAUD_AUTHOR": "Workshop User",
    }
    assert "PLAUD_AUTHORIZATION" not in os.environ
    assert "PLAUD_COOKIE" not in os.environ
    assert database.read_bytes() == b"database-sentinel"
    assert transcript.read_text(encoding="utf-8") == "transcript-sentinel"


def test_community_disconnect_refuses_another_keychain_namespace(
    tmp_path: Path,
    fake_keychain: FakeKeychain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_env = tmp_path / "settings.env"
    write_env_file({"PLAUD_AUTHORIZATION": "keep-placeholder"}, settings_env)
    fake_keychain.stored = store_mod._serialize({"PLAUD_AUTHORIZATION": "keychain-placeholder"})
    monkeypatch.setattr(store_mod, "KEYCHAIN_SERVICE", "com.example.PrivateApplication.auth")

    with pytest.raises(CredentialStoreError, match="Community credential namespace"):
        disconnect_community_credentials(settings_env)

    assert fake_keychain.stored is not None
    assert read_env_file(settings_env) == {"PLAUD_AUTHORIZATION": "keep-placeholder"}


def test_community_disconnect_accepts_windows_lite_namespace(
    tmp_path: Path,
    fake_keychain: FakeKeychain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_env = tmp_path / "settings.env"
    write_env_file({"PLAUD_AUTHORIZATION": "legacy-placeholder"}, settings_env)
    fake_keychain.stored = store_mod._serialize({"PLAUD_AUTHORIZATION": "credential-placeholder"})
    monkeypatch.setattr(
        store_mod,
        "KEYCHAIN_SERVICE",
        store_mod.WINDOWS_COMMUNITY_KEYCHAIN_SERVICE,
    )
    monkeypatch.setattr(
        store_mod,
        "APP_SUPPORT_ID",
        store_mod.WINDOWS_COMMUNITY_APP_SUPPORT_ID,
    )

    disconnect_community_credentials(settings_env)

    assert fake_keychain.stored is None
    assert read_env_file(settings_env) == {}
