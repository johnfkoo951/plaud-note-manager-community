from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_auto_refresh(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Keep the load_config auto-refresh hook inert during tests.

    A fixture that plants PLAUD_WS_REFRESH_TOKEN next to an expired
    PLAUD_AUTHORIZATION would otherwise make load_config fire a real network
    POST. Tests that exercise the hook re-enable it explicitly with mocks.
    """
    monkeypatch.setenv("PLAUD_AUTO_REFRESH", "0")
    # Production defaults to macOS Keychain with no plaintext fallback. Tests
    # use isolated temp .env files and must never touch the developer Keychain.
    monkeypatch.setenv("PLAUD_SECRET_STORE", "test-file")
    # API/refresh rejection tests persist a small server-verdict memo. Keep it
    # beside the test's isolated credentials, never in the live app data dir.
    import core.auth_status as auth_mod

    monkeypatch.setattr(auth_mod, "REJECTION_FILE", tmp_path / "auth_state.json")
