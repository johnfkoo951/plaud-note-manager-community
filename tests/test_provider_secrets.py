from __future__ import annotations

import errno
import json
import multiprocessing
import os
import stat
import threading

import pytest
from typer.testing import CliRunner

import core.provider_secrets as provider_secrets
import core.secret_store as secret_store
from cli.main import app


def _hold_provider_lock(ready, release) -> None:
    with provider_secrets._provider_secret_lock("elevenlabs"):
        ready.set()
        if not release.wait(10):
            raise RuntimeError("test lock holder timed out")


def _enter_provider_lock(trying, entered) -> None:
    trying.set()
    with provider_secrets._provider_secret_lock("elevenlabs"):
        entered.set()


@pytest.fixture(autouse=True)
def isolated_provider_lock_directory(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("PLAUD_PROVIDER_LOCK_DIRECTORY", str(tmp_path / "provider-locks"))
    monkeypatch.setenv("PLAUD_AUTH_BLOB_FILE", str(tmp_path / "config" / "auth.bin"))


@pytest.fixture
def fake_native_store(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str, str], str]:
    stored: dict[tuple[str, str, str], str] = {}

    def identity(*, service: str, account: str, windows_path) -> tuple[str, str, str]:
        return service, account, str(windows_path)

    def read(*, service: str, account: str, windows_path):
        return stored.get(identity(service=service, account=account, windows_path=windows_path))

    def replace(payload: str, *, service: str, account: str, windows_path) -> None:
        stored[identity(service=service, account=account, windows_path=windows_path)] = payload

    def delete(*, service: str, account: str, windows_path) -> None:
        stored.pop(identity(service=service, account=account, windows_path=windows_path), None)

    monkeypatch.setattr(secret_store, "_native_read", read)
    monkeypatch.setattr(secret_store, "_native_replace", replace)
    monkeypatch.setattr(secret_store, "_native_delete", delete)
    return stored


def test_provider_keys_round_trip_in_separate_native_items(fake_native_store) -> None:
    provider_secrets.set_api_key("elevenlabs", "test-eleven-secret")
    provider_secrets.set_api_key("openai", "test-openai-secret")

    assert provider_secrets.get_api_key("elevenlabs") == "test-eleven-secret"
    assert provider_secrets.get_api_key("openai") == "test-openai-secret"
    assert len(fake_native_store) == 2
    assert provider_secrets.delete_api_key("elevenlabs") is True
    assert provider_secrets.get_api_key("elevenlabs") is None
    assert provider_secrets.get_api_key("openai") == "test-openai-secret"


def test_set_holds_provider_lock_through_readback_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: dict[str, str] = {}
    replacement_written = threading.Event()
    permit_verification = threading.Event()
    getter_started = threading.Event()
    getter_finished = threading.Event()
    errors: list[BaseException] = []
    fetched: list[str | None] = []

    def replace(payload: str, **_kwargs) -> None:
        stored["payload"] = payload
        replacement_written.set()
        if not permit_verification.wait(5):
            raise RuntimeError("test verification gate timed out")

    def read(**_kwargs):
        return stored.get("payload")

    def set_key() -> None:
        try:
            provider_secrets.set_api_key("elevenlabs", "test-linear-secret")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def get_key() -> None:
        getter_started.set()
        try:
            fetched.append(provider_secrets.get_api_key("elevenlabs"))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            getter_finished.set()

    monkeypatch.setattr(secret_store, "_native_replace", replace)
    monkeypatch.setattr(secret_store, "_native_read", read)

    setter = threading.Thread(target=set_key, name="provider-setter", daemon=True)
    getter = threading.Thread(target=get_key, name="provider-getter", daemon=True)
    setter.start()
    assert replacement_written.wait(5)
    getter.start()
    assert getter_started.wait(5)
    try:
        assert not getter_finished.wait(0.2)
    finally:
        permit_verification.set()
    setter.join(5)
    getter.join(5)

    assert not setter.is_alive()
    assert not getter.is_alive()
    assert errors == []
    assert fetched == ["test-linear-secret"]


def test_provider_lock_excludes_a_second_process() -> None:
    context = multiprocessing.get_context("spawn")
    holder_ready = context.Event()
    holder_release = context.Event()
    contender_trying = context.Event()
    contender_entered = context.Event()
    holder = context.Process(target=_hold_provider_lock, args=(holder_ready, holder_release))
    contender = context.Process(
        target=_enter_provider_lock,
        args=(contender_trying, contender_entered),
    )
    processes = (holder, contender)

    try:
        holder.start()
        assert holder_ready.wait(10)
        contender.start()
        assert contender_trying.wait(10)
        assert not contender_entered.wait(0.3)
        holder_release.set()
        assert contender_entered.wait(10)
    finally:
        holder_release.set()
        for process in processes:
            if process.pid is None:
                continue
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)

    assert holder.exitcode == 0
    assert contender.exitcode == 0


def test_provider_lock_path_is_opaque_private_and_regular() -> None:
    target = provider_secrets._target("elevenlabs")

    assert "elevenlabs" not in target.lock_path.name
    with provider_secrets._provider_secret_lock("elevenlabs") as locked:
        assert locked == target
        assert stat.S_ISREG(target.lock_path.stat().st_mode)

    contents = target.lock_path.read_bytes()
    assert b"test-secret" not in contents
    assert contents in (b"", b"\0")
    if os.name == "posix":
        assert stat.S_IMODE(target.lock_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(target.lock_path.stat().st_mode) == 0o600


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is unavailable")
def test_provider_lock_rejects_a_symlink() -> None:
    target = provider_secrets._target("elevenlabs")
    target.lock_path.parent.mkdir(parents=True, mode=0o700)
    decoy = target.lock_path.parent / "decoy"
    decoy.write_bytes(b"")
    target.lock_path.symlink_to(decoy)

    with pytest.raises(secret_store.CredentialStoreError, match="lock is unavailable"):
        with provider_secrets._provider_secret_lock("elevenlabs"):
            pass


def test_mocked_windows_provider_lock_retries_then_unlocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def __init__(self) -> None:
            self.attempts = 0

        def locking(self, _fd: int, mode: int, length: int) -> None:
            calls.append((mode, length))
            if mode == self.LK_NBLCK:
                self.attempts += 1
                if self.attempts == 1:
                    raise OSError(errno.EACCES, "busy")

    fake = FakeMsvcrt()
    monkeypatch.setattr(provider_secrets.sys, "platform", "win32")
    monkeypatch.setattr(provider_secrets, "_load_msvcrt", lambda: fake)
    monkeypatch.setattr(provider_secrets.time, "sleep", lambda _seconds: None)

    with provider_secrets._provider_secret_lock("elevenlabs") as target:
        assert target.lock_path.read_bytes() == b"\0"

    assert calls == [
        (fake.LK_NBLCK, 1),
        (fake.LK_NBLCK, 1),
        (fake.LK_UNLCK, 1),
    ]


def test_mocked_windows_provider_lock_has_a_bounded_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BusyMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(_fd: int, _mode: int, _length: int) -> None:
            raise OSError(errno.EACCES, "busy")

    after_deadline = 31.0
    clock = iter((0.0, after_deadline))
    monkeypatch.setattr(provider_secrets.sys, "platform", "win32")
    monkeypatch.setattr(provider_secrets, "_load_msvcrt", lambda: BusyMsvcrt())
    monkeypatch.setattr(provider_secrets.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(provider_secrets.time, "sleep", lambda _seconds: None)

    assert after_deadline > provider_secrets._WINDOWS_LOCK_TIMEOUT_SECONDS
    with pytest.raises(secret_store.CredentialStoreError, match="timed out"):
        with provider_secrets._provider_secret_lock("elevenlabs"):
            pass


def test_windows_provider_blob_is_namespaced_beside_not_inside_plaud_auth(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provider_secrets.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    auth_blob = tmp_path / "CMDSPACE" / "PlaudNoteManagerCommunityLite" / "config" / "auth.bin"
    monkeypatch.setenv("PLAUD_AUTH_BLOB_FILE", str(auth_blob))

    target = provider_secrets._target("elevenlabs")

    assert target.service.endswith("WindowsLite.auth.providers")
    assert target.account == "api-key:elevenlabs"
    assert target.windows_path == (auth_blob.parent / "provider-secrets" / "elevenlabs.bin")
    assert target.windows_path.name != "auth.bin"


def test_provider_status_never_contains_key_fragment(fake_native_store) -> None:
    secret = "super-secret-value-123456"
    provider_secrets.set_api_key("elevenlabs", secret)

    status = provider_secrets.api_key_status("elevenlabs")
    encoded = json.dumps(status)

    assert status == {
        "provider": "elevenlabs",
        "configured": True,
        "status": "set",
        "secret_disclosed": False,
    }
    assert secret not in encoded
    assert secret[:4] not in encoded
    assert secret[-4:] not in encoded


def test_provider_unset_status_has_no_secret_placeholder(fake_native_store) -> None:
    assert provider_secrets.api_key_status("elevenlabs") == {
        "provider": "elevenlabs",
        "configured": False,
        "status": "unset",
        "secret_disclosed": False,
    }


@pytest.mark.parametrize("bad", ["", "unknown", "../elevenlabs", "eleven labs"])
def test_provider_allowlist_rejects_unscoped_names(bad: str) -> None:
    with pytest.raises(ValueError, match="unsupported API-key provider"):
        provider_secrets.get_api_key(bad)


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "two keys", "line1\nline2", "비밀키", "x" * 4097],
)
def test_provider_key_validation_rejects_ambiguous_input(fake_native_store, bad: str) -> None:
    with pytest.raises(ValueError):
        provider_secrets.set_api_key("elevenlabs", bad)
    assert fake_native_store == {}


def test_cli_reads_key_from_stdin_without_echo_or_mask(fake_native_store) -> None:
    secret = "test-cli-secret-987654321"

    stored = CliRunner().invoke(app, ["provider-key-set", "elevenlabs"], input=secret + "\n")
    status = CliRunner().invoke(app, ["provider-key-status", "elevenlabs", "--json"])

    assert stored.exit_code == 0, stored.output
    assert status.exit_code == 0, status.output
    combined = stored.output + status.output
    assert secret not in combined
    assert secret[:4] not in combined
    assert secret[-4:] not in combined
    assert json.loads(status.output) == {
        "provider": "elevenlabs",
        "configured": True,
        "status": "set",
        "secret_disclosed": False,
    }


def test_cli_delete_reports_only_state(fake_native_store) -> None:
    provider_secrets.set_api_key("elevenlabs", "test-delete-secret")

    result = CliRunner().invoke(app, ["provider-key-delete", "elevenlabs"])

    assert result.exit_code == 0, result.output
    assert "removed" in result.output
    assert "test-delete-secret" not in result.output
    assert not provider_secrets.has_api_key("elevenlabs")


def test_delete_recovers_from_unreadable_provider_blob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = iter([secret_store.CredentialStoreError("corrupted"), None])
    deleted: list[dict] = []

    def fake_read(**_kwargs):
        result = next(reads)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(provider_secrets.secret_store, "_native_read", fake_read)
    monkeypatch.setattr(
        provider_secrets.secret_store,
        "_native_delete",
        lambda **kwargs: deleted.append(kwargs),
    )

    assert provider_secrets.delete_api_key("elevenlabs") is True
    assert len(deleted) == 1
