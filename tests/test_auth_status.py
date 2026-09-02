"""Tests for core.auth_status — JWT decode + expiry classification (no network)."""

from __future__ import annotations

import base64
import json

import core.auth_status as auth_mod
import core.client as client_mod
from core.auth_status import _decode_jwt_payload, _human_duration, auth_status, mask_id
from core.client import PlaudAPIError
from core.config import ConfigError, PlaudConfig


def test_mask_id():
    assert mask_id("ws_testWkspc9") == "ws_tes…pc9"
    assert mask_id("mem_testMembr8") == "mem_te…br8"
    assert mask_id(None) is None
    assert mask_id("") == ""
    # short values don't reveal most of themselves
    assert mask_id("abcd") == "…bcd"


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _make_jwt(payload: dict) -> str:
    return f"{_b64({'alg': 'HS256', 'typ': 'JWT'})}.{_b64(payload)}.sig"


def _fake_config(token: str):
    return PlaudConfig(
        authorization=f"bearer {token}",
        x_device_id="dev",
        x_pld_user="user",
    )


def test_decode_jwt_payload_round_trip():
    claims = {"exp": 123, "iat": 1, "wid": "ws_x"}
    assert _decode_jwt_payload(_make_jwt(claims)) == claims


def test_decode_jwt_payload_rejects_non_jwt():
    assert _decode_jwt_payload("not-a-jwt") is None
    assert _decode_jwt_payload("only.two") is None


def test_human_duration():
    assert _human_duration(0) == "expired"
    assert _human_duration(-5) == "expired"
    assert _human_duration(90061) == "1d 1h"
    assert _human_duration(3700) == "1h 1m"
    assert _human_duration(120) == "2m"


def test_unconfigured(monkeypatch):
    def boom():
        raise ConfigError("missing creds")

    monkeypatch.setattr(auth_mod, "load_config", boom)
    st = auth_status(now=1000)
    assert st.configured is False
    assert st.state == "unconfigured"


def test_valid_token(monkeypatch):
    now = 1_000_000
    token = _make_jwt(
        {
            "exp": now + 50_000,
            "iat": now - 100,
            "wid": "ws_testWkspc9",
            "mid": "mem_testMembr8",
            "role": "admin",
        }
    )
    monkeypatch.setattr(auth_mod, "load_config", lambda: _fake_config(token))
    st = auth_status(now=now)
    assert st.state == "valid"
    assert st.workspace_id == "ws_tes…pc9"  # masked at source
    assert st.member_id == "mem_te…br8"
    assert st.role == "admin"
    assert st.seconds_remaining == 50_000
    assert st.live_ok is None  # not pinged


def test_expiring_soon(monkeypatch):
    now = 1_000_000
    token = _make_jwt({"exp": now + 600, "iat": now - 100})  # < 2h window
    monkeypatch.setattr(auth_mod, "load_config", lambda: _fake_config(token))
    assert auth_status(now=now).state == "expiring"


def test_expired(monkeypatch):
    now = 1_000_000
    token = _make_jwt({"exp": now - 10, "iat": now - 90_000})
    monkeypatch.setattr(auth_mod, "load_config", lambda: _fake_config(token))
    assert auth_status(now=now).state == "expired"


def test_non_jwt_token_is_unknown(monkeypatch):
    monkeypatch.setattr(auth_mod, "load_config", lambda: _fake_config("opaque-token"))
    st = auth_status(now=1000)
    assert st.configured is True
    assert st.state == "unknown"


def _stub_client(monkeypatch, *, error: Exception | None = None) -> dict:
    """Patch PlaudClient at its import site (core.client) with a recording stub."""
    record: dict = {"calls": 0}

    class FakeClient:
        def __init__(self, cfg, *, timeout=30.0):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def list_files(self, *, limit):
            record["calls"] += 1
            if error is not None:
                raise error
            return None

    monkeypatch.setattr(client_mod, "PlaudClient", FakeClient)
    return record


def test_live_probe_401_is_rejected(monkeypatch):
    now = 1_000_000
    token = _make_jwt({"exp": now + 50_000, "iat": now - 100})
    monkeypatch.setattr(auth_mod, "load_config", lambda: _fake_config(token))
    _stub_client(monkeypatch, error=PlaudAPIError("Plaud HTTP 401", status_code=401))
    st = auth_status(live=True, now=now)
    assert st.live_state == "rejected"
    assert st.live_ok is False


def test_live_probe_network_error_is_unreachable(monkeypatch):
    now = 1_000_000
    token = _make_jwt({"exp": now + 50_000, "iat": now - 100})
    monkeypatch.setattr(auth_mod, "load_config", lambda: _fake_config(token))
    _stub_client(monkeypatch, error=PlaudAPIError("network down"))  # no status_code
    st = auth_status(live=True, now=now)
    assert st.live_state == "unreachable"
    assert st.live_ok is None  # can't verify ≠ rejected


def test_undecodable_jwt_still_runs_live_probe(monkeypatch):
    monkeypatch.setattr(auth_mod, "load_config", lambda: _fake_config("opaque-token"))
    record = _stub_client(monkeypatch)
    st = auth_status(live=True, now=1000)
    assert record["calls"] == 1  # probe ran despite the opaque token
    assert st.state == "unknown"
    assert st.live_state == "ok"
    assert st.live_ok is True


# --- Server-verdict memo ------------------------------------------------------
#
# Regression cover for the 2026-08-19 failure: a web.plaud.ai sign-in rotated
# the workspace chain, so the server started answering -419 while the stored
# JWT still claimed ~23h of life. Every layer read the JWT and believed it —
# `auth --json` said "valid", the app's dot stayed green, and `auth-recover`'s
# precheck returned "fresh" and stopped without recovering. The memo below is
# what makes the server's verdict outrank the token's own claim.


def _memo_at(tmp_path, monkeypatch):
    """Point the rejection memo at a temp file, never the real data dir."""
    path = tmp_path / "auth_state.json"
    monkeypatch.setattr(auth_mod, "REJECTION_FILE", path)
    return path


def test_recorded_rejection_overrides_a_still_valid_jwt(tmp_path, monkeypatch):
    _memo_at(tmp_path, monkeypatch)
    token = _make_jwt({"iat": 1000, "exp": 1000 + 86400, "wid": "ws_x", "mid": "mem_y"})
    monkeypatch.setattr(auth_mod, "load_config", lambda: _fake_config(token))

    # Before the server says anything, the JWT is trusted.
    assert auth_status(now=2000).state == "valid"

    auth_mod.record_auth_rejection(status=-419, now=1500)
    status = auth_status(now=2000)
    assert status.state == "rejected"
    assert "server rejected" in status.detail


def test_rejection_older_than_the_token_is_ignored(tmp_path, monkeypatch):
    """A memo from the *previous* credential must not condemn its replacement."""
    _memo_at(tmp_path, monkeypatch)
    auth_mod.record_auth_rejection(status=-419, now=500)
    token = _make_jwt({"iat": 1000, "exp": 1000 + 86400})
    monkeypatch.setattr(auth_mod, "load_config", lambda: _fake_config(token))

    assert auth_status(now=2000).state == "valid"


def test_clear_auth_rejection_restores_trust(tmp_path, monkeypatch):
    _memo_at(tmp_path, monkeypatch)
    token = _make_jwt({"iat": 1000, "exp": 1000 + 86400})
    monkeypatch.setattr(auth_mod, "load_config", lambda: _fake_config(token))

    auth_mod.record_auth_rejection(status=-419, now=1500)
    assert auth_status(now=2000).state == "rejected"
    auth_mod.clear_auth_rejection()
    assert auth_status(now=2000).state == "valid"
    assert auth_mod.auth_rejected_at() is None


def test_expired_state_is_not_masked_by_the_memo(tmp_path, monkeypatch):
    """An outright expiry stays "expired" — it has its own recovery path."""
    _memo_at(tmp_path, monkeypatch)
    token = _make_jwt({"iat": 1000, "exp": 1100})
    monkeypatch.setattr(auth_mod, "load_config", lambda: _fake_config(token))
    auth_mod.record_auth_rejection(status=-419, now=1500)

    assert auth_status(now=2000).state == "expired"


def test_minus_420_counts_as_an_auth_rejection():
    """-420 (refresh token refused) used to be classified as transient, which
    kept the recovery ladder from escalating to a browser re-harvest."""
    assert PlaudAPIError("x", api_status=-420).is_auth_rejection
    assert PlaudAPIError("x", api_status=-419).is_auth_rejection
    assert not PlaudAPIError("x", api_status=-1).is_auth_rejection
