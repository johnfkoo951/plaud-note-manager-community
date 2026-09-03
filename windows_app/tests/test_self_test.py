from __future__ import annotations

import sys

from windows_app import launcher
from windows_app.self_test import _verify_dpapi_round_trip, run_self_test


def test_packaged_self_test_is_offline_and_passes(capsys, tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    launcher.configure_environment(
        local_app_data=tmp_path / "LocalAppData",
        resource_root=resources,
    )
    assert run_self_test() == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("Windows Community Lite self-test: PASS")
    if sys.platform == "win32":
        assert "HTTP + DPAPI" in captured.out
    else:
        assert "DPAPI requires Windows" in captured.out
    assert captured.err == ""


def test_dpapi_contract_checks_round_trip_ciphertext_and_delete(tmp_path):
    blob_path = tmp_path / "auth.bin"
    memory = {"plain": None}

    def replace(payload):
        memory["plain"] = payload
        raw = payload.encode("utf-8")
        blob_path.write_bytes(bytes(value ^ 0xA5 for value in raw))

    def read():
        return memory["plain"]

    def delete():
        memory["plain"] = None
        blob_path.unlink(missing_ok=True)

    _verify_dpapi_round_trip(
        native_replace=replace,
        native_read=read,
        native_delete=delete,
        blob_path=blob_path,
    )
    assert not blob_path.exists()


def test_dpapi_contract_rejects_plaintext_blob(tmp_path):
    blob_path = tmp_path / "auth.bin"
    memory = {"plain": None}

    def replace(payload):
        memory["plain"] = payload
        blob_path.write_text(payload, encoding="utf-8")

    def read():
        return memory["plain"]

    def delete():
        memory["plain"] = None
        blob_path.unlink(missing_ok=True)

    try:
        _verify_dpapi_round_trip(
            native_replace=replace,
            native_read=read,
            native_delete=delete,
            blob_path=blob_path,
        )
    except RuntimeError as exc:
        assert str(exc) == "credential blob contains plaintext"
    else:
        raise AssertionError("plaintext credential blob was accepted")
    assert not blob_path.exists()
