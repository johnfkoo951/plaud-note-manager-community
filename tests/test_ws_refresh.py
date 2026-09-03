"""Tests for headless workspace-token refresh (core.ws_refresh) and the
merge-safe .env writer it depends on (core.config)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx
import pytest

import core.config as config_mod
import core.ws_refresh as ws_mod
from core.auth_status import auth_status
from core.config import read_env_file, update_env_file, write_env_file
from core.ws_refresh import (
    REFRESH_WHEN_REMAINING,
    RefreshResult,
    apply_refresh_result,
    bootstrap_workspace,
    credential_env_updates,
    ensure_fresh_token,
    parse_workspace_list,
    refresh_workspace_token,
    select_workspace_entry,
)
from tests.test_auth_status import _make_jwt

NOW = 1_800_000_000


def _clear_plaud_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """os.environ is a fallback source for ws_refresh — keep it out of the way."""
    for key in list(os.environ):
        if key.startswith("PLAUD_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLAUD_AUTO_REFRESH", "0")
    monkeypatch.setenv("PLAUD_SECRET_STORE", "test-file")


def _seed_env(
    path: Path,
    *,
    auth_exp: int | None,
    ws_token: str | None = "refresh-1",
    extra: dict[str, str] | None = None,
) -> None:
    values = {
        "PLAUD_AUTHORIZATION": "bearer " + _make_jwt({"exp": auth_exp, "wid": "ws_abc"})
        if auth_exp is not None
        else "bearer not-a-jwt",
        "PLAUD_X_DEVICE_ID": "dev-1",
        "PLAUD_X_PLD_USER": "user-1",
        "PLAUD_WORKSPACE_ID": "ws_abc",
    }
    if ws_token:
        values["PLAUD_WS_REFRESH_TOKEN"] = ws_token
    values.update(extra or {})
    write_env_file(values, path)


# ---------------------------------------------------------------- env writer


def test_update_env_file_merges_and_deletes(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    write_env_file({"A": "1", "B": "keep me", "C": "gone"}, env)
    update_env_file({"A": "2", "C": None, "D": "new"}, env)
    assert read_env_file(env) == {"A": "2", "B": "keep me", "D": "new"}


def test_env_file_quoting_round_trip(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    tricky = "it's a \\ 'quoted' value"
    write_env_file({"K": tricky}, env)
    assert read_env_file(env)["K"] == tricky


def test_env_writer_keeps_0600_and_no_temp_left_behind(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    write_env_file({"A": "1"}, env)
    update_env_file({"B": "2"}, env)
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != ".env"]
    assert leftovers == []


def test_read_env_file_tolerates_hand_edits(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# comment\n\nexport A=plain\nB='quoted'\nnoequals\n")
    assert read_env_file(env) == {"A": "plain", "B": "quoted"}


# ------------------------------------------------------- apply_refresh_result


def test_apply_refresh_result_persists_rotation(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW - 10)
    new_token = _make_jwt({"exp": NOW + 86_400, "wid": "ws_abc"})
    outcome = apply_refresh_result(
        RefreshResult(new_token, 86_400, "refresh-2", 30 * 86_400), env, now=NOW
    )
    assert outcome.status == "ok"
    assert outcome.access_expires_at == NOW + 86_400
    assert outcome.refresh_expires_at == NOW + 30 * 86_400
    assert not outcome.refresh_expiring_soon
    values = read_env_file(env)
    assert values["PLAUD_AUTHORIZATION"] == f"bearer {new_token}"
    assert values["PLAUD_WS_REFRESH_TOKEN"] == "refresh-2"  # rotation persisted
    assert values["PLAUD_X_DEVICE_ID"] == "dev-1"  # untouched keys survive


def test_apply_refresh_result_keeps_previous_horizon_when_server_omits(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW - 10, extra={"PLAUD_WS_REFRESH_EXPIRES_AT": str(NOW + 999)})
    outcome = apply_refresh_result(
        RefreshResult(
            _make_jwt({"exp": NOW + 86_400, "wid": "ws_abc"}),
            86_400,
            "refresh-2",
            None,
        ),
        env,
        now=NOW,
    )
    assert outcome.refresh_expires_at == NOW + 999
    assert read_env_file(env)["PLAUD_WS_REFRESH_EXPIRES_AT"] == str(NOW + 999)


def test_apply_refresh_result_flags_expiring_refresh_token(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW - 10)
    outcome = apply_refresh_result(
        RefreshResult(
            _make_jwt({"exp": NOW + 86_400, "wid": "ws_abc"}),
            86_400,
            "refresh-2",
            3600,
        ),
        env,
        now=NOW,
    )
    assert outcome.refresh_expiring_soon


def test_apply_refresh_result_refuses_non_jwt(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW - 10)
    before = read_env_file(env)
    outcome = apply_refresh_result(RefreshResult("garbage", 86_400, "r2", None), env, now=NOW)
    assert outcome.status == "rejected"
    assert read_env_file(env) == before  # .env untouched


# ------------------------------------------------------------ workspaceList


def test_parse_workspace_list_variants() -> None:
    entry = {
        "workspaceId": "ws_abc",
        "refreshToken": "tok",
        "refreshExpiresAt": NOW * 1000,
        "domain": "api-apne1.plaud.ai",
    }
    for text in (
        json.dumps([entry]),
        json.dumps(entry),  # single object
        json.dumps(json.dumps([entry])),  # double-encoded
    ):
        entries = parse_workspace_list(text)
        assert len(entries) == 1
        assert entries[0].workspace_id == "ws_abc"
        assert entries[0].refresh_expires_at == NOW  # millis normalized to seconds


def test_parse_workspace_list_skips_incomplete_and_rejects_non_json() -> None:
    assert parse_workspace_list(json.dumps([{"workspaceId": "ws_x"}])) == []
    with pytest.raises(ValueError):
        parse_workspace_list("not json")


@pytest.mark.parametrize(
    "domain",
    [
        "http://api.plaud.ai",
        "https://plaud.ai.evil.example",
        "https://api.plaud.ai.evil.example",
        "https://user@api.plaud.ai",
        "https://api.plaud.ai:8443",
        "https://api.plaud.ai/redirect",
    ],
)
def test_parse_workspace_list_rejects_untrusted_api_domains(domain: str) -> None:
    payload = [{"workspaceId": "ws_abc", "refreshToken": "tok", "domain": domain}]
    with pytest.raises(ValueError, match="HTTPS api"):
        parse_workspace_list(json.dumps(payload))


def test_select_workspace_entry_rules() -> None:
    entries = parse_workspace_list(
        json.dumps(
            [
                {"workspaceId": "ws_abc", "refreshToken": "a"},
                {"workspaceId": "ws_def", "refreshToken": "b"},
            ]
        )
    )
    assert select_workspace_entry(entries, wid="ws_def").refresh_token == "b"
    assert select_workspace_entry(entries, wid="ws_zzz") is None
    assert select_workspace_entry(entries, wid=None) is None  # ambiguous without wid
    assert select_workspace_entry(entries[:1], wid=None).refresh_token == "a"


# ------------------------------------------------- credential_env_updates


def test_capture_preserves_ws_keys_and_clears_stale_cookie(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    _seed_env(
        env,
        auth_exp=NOW - 10,
        extra={
            "PLAUD_COOKIE": "old-cookie",
            "PLAUD_WS_REFRESH_EXPIRES_AT": str(NOW + 30 * 86_400),
        },
    )
    captured = {
        "PLAUD_AUTHORIZATION": "bearer " + _make_jwt({"exp": NOW + 86_400, "wid": "ws_abc"}),
        "PLAUD_X_DEVICE_ID": "dev-2",
        "PLAUD_X_PLD_USER": "user-2",
        "PLAUD_BASE_URL": "https://api-apne1.plaud.ai",
    }
    update_env_file(credential_env_updates(captured, env, now=NOW), env)
    values = read_env_file(env)
    assert values["PLAUD_WS_REFRESH_TOKEN"] == "refresh-1"  # headless refresh survives
    assert "PLAUD_COOKIE" not in values  # stale cookie does not outlive the login
    assert values["PLAUD_X_DEVICE_ID"] == "dev-2"


def test_capture_drops_ws_keys_when_workspace_changes(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    _seed_env(
        env,
        auth_exp=NOW - 10,
        extra={"PLAUD_WS_REFRESH_EXPIRES_AT": str(NOW + 30 * 86_400)},
    )
    captured = {
        "PLAUD_AUTHORIZATION": "bearer " + _make_jwt({"exp": NOW + 86_400, "wid": "ws_OTHER"}),
        "PLAUD_X_DEVICE_ID": "dev-2",
        "PLAUD_X_PLD_USER": "user-2",
    }
    update_env_file(credential_env_updates(captured, env, now=NOW), env)
    values = read_env_file(env)
    assert "PLAUD_WS_REFRESH_TOKEN" not in values
    assert "PLAUD_WORKSPACE_ID" not in values


@pytest.mark.parametrize(
    "binding_case",
    ["opaque_candidate", "expired_refresh", "missing_horizon", "missing_workspace_id"],
)
def test_capture_drops_refresh_binding_that_cannot_be_proven_safe(
    binding_case: str, tmp_path: Path
) -> None:
    env = tmp_path / ".env"
    expiry: int | None = NOW + 30 * 86_400
    candidate_authorization = "bearer " + _make_jwt({"exp": NOW + 86_400, "wid": "ws_abc"})
    if binding_case == "opaque_candidate":
        candidate_authorization = "bearer opaque.token.value"
    elif binding_case == "expired_refresh":
        expiry = NOW - 1
    elif binding_case == "missing_horizon":
        expiry = None

    extra = {"PLAUD_WS_REFRESH_EXPIRES_AT": str(expiry)} if expiry is not None else None
    _seed_env(env, auth_exp=NOW - 10, extra=extra)
    if binding_case == "missing_workspace_id":
        update_env_file({"PLAUD_WORKSPACE_ID": None}, env)
    captured = {
        "PLAUD_AUTHORIZATION": candidate_authorization,
        "PLAUD_X_DEVICE_ID": "dev-2",
    }

    update_env_file(credential_env_updates(captured, env, now=NOW), env)

    values = read_env_file(env)
    assert "PLAUD_WORKSPACE_ID" not in values
    assert "PLAUD_WS_REFRESH_TOKEN" not in values
    assert "PLAUD_WS_REFRESH_EXPIRES_AT" not in values


def test_capture_arms_from_workspace_list(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW - 10, ws_token=None)
    captured = {
        "PLAUD_AUTHORIZATION": "bearer " + _make_jwt({"exp": NOW + 86_400, "wid": "ws_abc"}),
        "PLAUD_X_DEVICE_ID": "dev-2",
        "PLAUD_X_PLD_USER": "user-2",
    }
    ws_list = json.dumps(
        [
            {
                "workspaceId": "ws_abc",
                "refreshToken": "fresh-tok",
                "refreshExpiresAt": NOW + 30 * 86_400,
                "domain": "api-eu1.plaud.ai",
            }
        ]
    )
    updates = credential_env_updates(captured, env, workspace_list_json=ws_list, now=NOW)
    update_env_file(updates, env)
    values = read_env_file(env)
    assert values["PLAUD_WS_REFRESH_TOKEN"] == "fresh-tok"
    assert values["PLAUD_BASE_URL"] == "https://api-eu1.plaud.ai"
    # malformed export is not fatal
    assert (
        credential_env_updates(captured, env, workspace_list_json="oops", now=NOW)[
            "PLAUD_X_DEVICE_ID"
        ]
        == "dev-2"
    )


def test_capture_does_not_arm_expired_workspace_list_entry(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW - 10, ws_token=None)
    captured = {
        "PLAUD_AUTHORIZATION": "bearer " + _make_jwt({"exp": NOW + 86_400, "wid": "ws_abc"}),
        "PLAUD_X_DEVICE_ID": "dev-2",
    }
    ws_list = json.dumps(
        [
            {
                "workspaceId": "ws_abc",
                "refreshToken": "expired-browser-token",
                "refreshExpiresAt": NOW - 1,
            }
        ]
    )

    update_env_file(
        credential_env_updates(captured, env, workspace_list_json=ws_list, now=NOW),
        env,
    )

    values = read_env_file(env)
    assert "PLAUD_WORKSPACE_ID" not in values
    assert "PLAUD_WS_REFRESH_TOKEN" not in values
    assert "PLAUD_WS_REFRESH_EXPIRES_AT" not in values


# ------------------------------------------------ refresh_workspace_token


def _fake_post(response_json: dict | None = None, *, status: int = 200, calls: list | None = None):
    def post(  # noqa: A002 - httpx kwargs
        url,
        json=None,
        headers=None,
        timeout=None,
        trust_env=True,
    ):
        if calls is not None:
            calls.append({"url": url, "headers": headers, "trust_env": trust_env})
        request = httpx.Request("POST", url)
        return httpx.Response(status, json=response_json or {}, request=request)

    return post


def test_refresh_ok_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_plaud_env(monkeypatch)
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW - 10)
    new_token = _make_jwt({"exp": NOW + 86_400, "wid": "ws_abc"})
    calls: list = []
    monkeypatch.setattr(
        ws_mod.httpx,
        "post",
        _fake_post(
            {
                "status": 0,
                "data": {
                    "workspace_token": new_token,
                    "expires_in": 86_400,
                    "refresh_token": "refresh-2",
                    "refresh_expires_in": 30 * 86_400,
                },
            },
            calls=calls,
        ),
    )
    outcome = refresh_workspace_token(env_path=env, now=NOW)
    assert outcome.status == "ok"
    assert calls[0]["url"].endswith("/user-app/auth/workspace/refresh/ws_abc")
    assert calls[0]["headers"]["Authorization"] == "bearer refresh-1"
    assert calls[0]["trust_env"] is False
    assert read_env_file(env)["PLAUD_WS_REFRESH_TOKEN"] == "refresh-2"


def test_refresh_only_if_needed_skips_network_when_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_plaud_env(monkeypatch)
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW + REFRESH_WHEN_REMAINING + 3600)

    def explode(*a, **k):
        raise AssertionError("network must not be touched")

    monkeypatch.setattr(ws_mod.httpx, "post", explode)
    outcome = refresh_workspace_token(env_path=env, now=NOW, only_if_needed=True)
    assert outcome.status == "fresh"


def test_refresh_rejected_disarms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_plaud_env(monkeypatch)
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW - 10)
    monkeypatch.setattr(ws_mod.httpx, "post", _fake_post({}, status=401))
    outcome = refresh_workspace_token(env_path=env, now=NOW)
    assert outcome.status == "rejected"
    values = read_env_file(env)
    assert "PLAUD_WS_REFRESH_TOKEN" not in values  # disarmed: no doomed retries
    assert values["PLAUD_WORKSPACE_ID"] == "ws_abc"  # kept for the next bootstrap


def test_refresh_unreachable_keeps_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_plaud_env(monkeypatch)
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW - 10)

    def net_down(url, **kwargs):
        raise httpx.ConnectError("boom", request=httpx.Request("POST", url))

    monkeypatch.setattr(ws_mod.httpx, "post", net_down)
    outcome = refresh_workspace_token(env_path=env, now=NOW)
    assert outcome.status == "unreachable"
    assert read_env_file(env)["PLAUD_WS_REFRESH_TOKEN"] == "refresh-1"


def test_refresh_not_bootstrapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_plaud_env(monkeypatch)
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW - 10, ws_token=None)
    assert refresh_workspace_token(env_path=env, now=NOW).status == "not_bootstrapped"


# --------------------------------------------------------- ensure_fresh_token


def test_ensure_fresh_token_gates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_plaud_env(monkeypatch)
    env = tmp_path / ".env"

    # Not bootstrapped -> None, no network.
    _seed_env(env, auth_exp=NOW - 10, ws_token=None)
    assert ensure_fresh_token(env_path=env, now=NOW) is None

    # Fresh token -> None, no network.
    _seed_env(env, auth_exp=NOW + REFRESH_WHEN_REMAINING + 3600)
    assert ensure_fresh_token(env_path=env, now=NOW) is None

    # Expiring -> refresh fires.
    _seed_env(env, auth_exp=NOW + 60)
    new_token = _make_jwt({"exp": NOW + 86_400, "wid": "ws_abc"})
    monkeypatch.setattr(
        ws_mod.httpx,
        "post",
        _fake_post(
            {
                "status": 0,
                "data": {
                    "workspace_token": new_token,
                    "expires_in": 86_400,
                    "refresh_token": "refresh-2",
                    "refresh_expires_in": None,
                },
            }
        ),
    )
    outcome = ensure_fresh_token(env_path=env, now=NOW)
    assert outcome is not None and outcome.status == "ok"


def test_load_config_hook_respects_opt_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_plaud_env(monkeypatch)
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW - 10)
    monkeypatch.setenv("PLAUD_ENV_FILE", str(env))
    called: list = []
    monkeypatch.setattr(ws_mod, "ensure_fresh_token", lambda **kw: called.append(kw))

    monkeypatch.setenv("PLAUD_AUTO_REFRESH", "0")
    config_mod.load_config()
    assert called == []

    monkeypatch.setenv("PLAUD_AUTO_REFRESH", "1")
    config_mod.load_config()
    assert len(called) == 1


def test_load_config_hook_swallows_refresh_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_plaud_env(monkeypatch)
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW - 10)
    monkeypatch.setenv("PLAUD_ENV_FILE", str(env))
    monkeypatch.setenv("PLAUD_AUTO_REFRESH", "1")

    def boom(**kwargs):
        raise RuntimeError("refresh plumbing exploded")

    monkeypatch.setattr(ws_mod, "ensure_fresh_token", boom)
    cfg = config_mod.load_config()  # must not raise
    assert cfg.x_device_id == "dev-1"


# --------------------------------------------------------- bootstrap_workspace


def test_bootstrap_workspace_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_plaud_env(monkeypatch)
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW - 10, ws_token=None)
    new_token = _make_jwt({"exp": NOW + 86_400, "wid": "ws_abc"})
    monkeypatch.setattr(
        ws_mod.httpx,
        "post",
        _fake_post(
            {
                "status": 0,
                "data": {
                    "workspace_token": new_token,
                    "expires_in": 86_400,
                    "refresh_token": "rotated",
                    "refresh_expires_in": 30 * 86_400,
                },
            }
        ),
    )
    text = json.dumps([{"workspaceId": "ws_abc", "refreshToken": "pasted-tok"}])
    outcome = bootstrap_workspace(text, env_path=env, now=NOW)
    assert outcome.status == "ok"
    values = read_env_file(env)
    assert values["PLAUD_WS_REFRESH_TOKEN"] == "rotated"  # validated AND rotated
    assert values["PLAUD_WORKSPACE_ID"] == "ws_abc"


def test_bootstrap_workspace_refuses_wrong_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_plaud_env(monkeypatch)
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW - 10, ws_token=None)
    text = json.dumps([{"workspaceId": "ws_OTHER", "refreshToken": "tok"}])
    outcome = bootstrap_workspace(text, env_path=env, now=NOW)
    assert outcome.status == "invalid_payload"
    assert "PLAUD_WS_REFRESH_TOKEN" not in read_env_file(env)


# -------------------------------------------------------- auth_status fields


def test_auth_status_reports_auto_refresh_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_plaud_env(monkeypatch)
    monkeypatch.setenv("PLAUD_AUTO_REFRESH", "1")
    env = tmp_path / ".env"
    monkeypatch.setenv("PLAUD_ENV_FILE", str(env))

    _seed_env(env, auth_exp=NOW + 86_400, ws_token=None)
    assert auth_status(now=NOW).auto_refresh == "not_bootstrapped"

    _seed_env(
        env, auth_exp=NOW + 86_400, extra={"PLAUD_WS_REFRESH_EXPIRES_AT": str(NOW + 30 * 86_400)}
    )
    st = auth_status(now=NOW)
    assert st.auto_refresh == "ready"
    assert st.refresh_expires_at == NOW + 30 * 86_400

    _seed_env(env, auth_exp=NOW + 86_400, extra={"PLAUD_WS_REFRESH_EXPIRES_AT": str(NOW + 3600)})
    assert auth_status(now=NOW).auto_refresh == "expiring"

    _seed_env(env, auth_exp=NOW + 86_400, extra={"PLAUD_WS_REFRESH_EXPIRES_AT": str(NOW - 5)})
    assert auth_status(now=NOW).auto_refresh == "expired"


def test_env_writer_neutralizes_newline_smuggling(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    write_env_file({"A": "evil\nB=injected", "C": "after"}, env)
    values = read_env_file(env)
    assert values == {"A": "evilB=injected", "C": "after"}  # no structural split


def test_only_if_needed_does_not_short_circuit_after_a_server_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`only_if_needed` compares the token's own `exp` against a threshold.

    That claim is worthless once the server has rejected the token — and
    trusting it is precisely what made `auth-recover` report "nothing to do"
    on 2026-08-19 while every API call came back -419. With a rejection on
    record, the refresh must actually go to the server.
    """
    import core.auth_status as auth_mod

    _clear_plaud_env(monkeypatch)
    monkeypatch.setattr(auth_mod, "REJECTION_FILE", tmp_path / "auth_state.json")
    auth_mod.record_auth_rejection(status=-419, now=NOW - 60)

    env = tmp_path / ".env"
    # A token well past the short-circuit threshold: without the memo this
    # returns "fresh" without touching the network (see the test above).
    _seed_env(env, auth_exp=NOW + REFRESH_WHEN_REMAINING + 3600)

    calls: list = []
    monkeypatch.setattr(
        ws_mod.httpx, "post", _fake_post({"status": -420, "msg": "nope"}, calls=calls)
    )

    outcome = refresh_workspace_token(env_path=env, now=NOW, only_if_needed=True)

    assert calls, "refresh must reach the server despite the fresh-looking JWT"
    # -420 is a rejection, so the ladder can climb to the browser re-harvest
    # instead of writing it off as a transient outage.
    assert outcome.status == "rejected"


def test_ensure_fresh_token_reaches_locked_refresh_after_server_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outer cheap precheck must not hide the rejection-aware inner path."""
    import core.auth_status as auth_mod

    _clear_plaud_env(monkeypatch)
    monkeypatch.setenv("PLAUD_AUTO_REFRESH", "1")
    auth_mod.record_auth_rejection(status=-419, now=NOW - 60)
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW + REFRESH_WHEN_REMAINING + 3600)
    calls: list = []
    monkeypatch.setattr(ws_mod.httpx, "post", _fake_post({"status": -420}, calls=calls))

    outcome = ws_mod.ensure_fresh_token(env_path=env, now=NOW)

    assert calls
    assert outcome is not None and outcome.status == "rejected"


def test_minus_420_disarms_dead_refresh_token_for_browser_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rotated-away token must not remain 'ready' or shadow a new capture."""
    _clear_plaud_env(monkeypatch)
    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW - 10)
    monkeypatch.setattr(ws_mod.httpx, "post", _fake_post({"status": -420}))

    outcome = refresh_workspace_token(env_path=env, now=NOW)

    assert outcome.status == "rejected"
    values = read_env_file(env)
    assert "PLAUD_WS_REFRESH_TOKEN" not in values
    assert "PLAUD_WS_REFRESH_EXPIRES_AT" not in values
    assert values["PLAUD_WORKSPACE_ID"] == "ws_abc"


def test_successful_refresh_clears_the_rejection_memo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new authorization retires the verdict passed on its predecessor."""
    import core.auth_status as auth_mod

    _clear_plaud_env(monkeypatch)
    monkeypatch.setattr(auth_mod, "REJECTION_FILE", tmp_path / "auth_state.json")
    auth_mod.record_auth_rejection(status=-419, now=NOW - 60)

    env = tmp_path / ".env"
    _seed_env(env, auth_exp=NOW - 10)
    monkeypatch.setattr(
        ws_mod.httpx,
        "post",
        _fake_post(
            {
                "status": 0,
                "data": {
                    "workspace_token": _make_jwt({"exp": NOW + 86400, "wid": "ws_abc"}),
                    "refresh_token": "refresh-2",
                    "expires_in": 86400,
                },
            }
        ),
    )

    outcome = refresh_workspace_token(env_path=env, now=NOW)

    assert outcome.status == "ok"
    assert auth_mod.auth_rejected_at() is None
