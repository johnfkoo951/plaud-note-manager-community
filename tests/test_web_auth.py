from __future__ import annotations

import json
import time

from typer.testing import CliRunner

import core.web_auth as web_auth_mod
import core.auth_status as auth_status_mod
from cli.main import app
from core.client import PlaudAPIError
from core.config import load_config
from core.web_auth import WebAuthCapture, import_web_auth
from tests.test_auth_status import _make_jwt


def _clear_auth_env(monkeypatch) -> None:
    for key in (
        "PLAUD_AUTHORIZATION",
        "PLAUD_X_DEVICE_ID",
        "PLAUD_X_PLD_USER",
        "PLAUD_X_PLD_TAG",
        "PLAUD_COOKIE",
        "PLAUD_BASE_URL",
        "PLAUD_APP_LANGUAGE",
        "PLAUD_APP_PLATFORM",
        "PLAUD_EDIT_FROM",
        "PLAUD_ORIGIN",
        "PLAUD_REFERER",
        "PLAUD_TIMEZONE",
        "PLAUD_WORKSPACE_ID",
        "PLAUD_WS_REFRESH_TOKEN",
        "PLAUD_WS_REFRESH_EXPIRES_AT",
    ):
        monkeypatch.delenv(key, raising=False)


def _fake_client(record: dict, *, error: Exception | None = None):
    """Stand-in for PlaudClient that records the candidate config it was given."""

    class FakeClient:
        def __init__(self, cfg, *, timeout: float = 30.0) -> None:
            record["authorization"] = cfg.authorization
            record["timeout"] = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *exc) -> None:
            return None

        def list_files(self, *, limit: int):
            record["limit"] = limit
            if error is not None:
                raise error
            return None

    return FakeClient


def test_import_web_auth_writes_env_from_structured_capture(tmp_path, monkeypatch) -> None:
    _clear_auth_env(monkeypatch)
    env_path = tmp_path / ".env"
    capture = WebAuthCapture.model_validate(
        {
            "authorization": "Bearer fresh.token.value",
            "x_device_id": "device-from-webkit",
            "x_pld_user": "user-from-webkit",
            "cookie": "session=abc; workspace=cmds",
            "timezone": "Asia/Seoul",
        }
    )

    result = import_web_auth(
        capture,
        env_path=env_path,
        live_validator=lambda values: "ok",
    )

    assert result.status == "ok"
    assert result.cookie_captured is True
    written = env_path.read_text(encoding="utf-8")
    assert "PLAUD_AUTHORIZATION='Bearer fresh.token.value'" in written
    assert "PLAUD_COOKIE='session=abc; workspace=cmds'" in written

    cfg = load_config(env_path)
    assert cfg.headers()["authorization"] == "Bearer fresh.token.value"
    assert cfg.headers()["cookie"] == "session=abc; workspace=cmds"
    assert cfg.headers()["x-device-id"] == "device-from-webkit"


def test_import_web_auth_writes_env_with_0600_permissions(tmp_path, monkeypatch) -> None:
    _clear_auth_env(monkeypatch)
    env_path = tmp_path / ".env"
    # Pre-existing world-readable .env must be tightened too, not just new files.
    env_path.write_text("PLAUD_AUTHORIZATION='old'\n", encoding="utf-8")
    env_path.chmod(0o644)

    result = import_web_auth(
        {
            "authorization": "Bearer fresh.token.value",
            "x_device_id": "device-from-webkit",
            "x_pld_user": "user-from-webkit",
        },
        env_path=env_path,
        live_validator=lambda values: "ok",
    )

    assert result.status == "ok"
    assert (env_path.stat().st_mode & 0o777) == 0o600


def test_import_web_auth_rejects_missing_required_headers(tmp_path) -> None:
    env_path = tmp_path / ".env"
    result = import_web_auth(
        {
            "authorization": "Bearer missing.user",
            "cookie": "session=abc",
        },
        env_path=env_path,
        live_validator=lambda values: "ok",
    )

    assert result.status == "missing_required"
    assert "x_device_id" in result.detail
    assert result.cookie_captured is True
    assert not env_path.exists()


def test_import_web_auth_invalid_payload_writes_nothing(tmp_path) -> None:
    env_path = tmp_path / ".env"
    result = import_web_auth(
        {"authorization": 123},  # type: ignore[dict-item]
        env_path=env_path,
        live_validator=lambda values: "ok",
    )

    assert result.status == "invalid_payload"
    assert not env_path.exists()


def test_import_web_auth_rejects_untrusted_base_url_before_live_probe(tmp_path) -> None:
    called = False

    def validator(values) -> str:
        nonlocal called
        called = True
        return "ok"

    result = import_web_auth(
        {
            "authorization": "Bearer fresh.token.value",
            "x_device_id": "device-from-webkit",
            "base_url": "https://api.plaud.ai.attacker.example",
        },
        env_path=tmp_path / ".env",
        live_validator=validator,
    )

    assert result.status == "invalid_payload"
    assert called is False
    assert not (tmp_path / ".env").exists()


def test_import_web_auth_accepts_header_only_capture_after_live_validation(
    tmp_path, monkeypatch
) -> None:
    _clear_auth_env(monkeypatch)
    env_path = tmp_path / ".env"

    result = import_web_auth(
        {
            "authorization": "Bearer header.only",
            "x_device_id": "device-from-webkit",
        },
        env_path=env_path,
        live_validator=lambda values: "ok",
    )

    assert result.status == "ok"
    assert result.cookie_captured is False
    written = env_path.read_text(encoding="utf-8")
    assert "PLAUD_AUTHORIZATION='Bearer header.only'" in written
    assert "PLAUD_COOKIE" not in written
    assert "PLAUD_X_PLD_USER" not in written
    assert "x-pld-user" not in load_config(env_path).headers()


def test_import_web_auth_rejected_never_creates_env(tmp_path) -> None:
    env_path = tmp_path / ".env"

    result = import_web_auth(
        {
            "authorization": "Bearer rejected.token",
            "x_device_id": "new-device",
            "x_pld_user": "new-user",
        },
        env_path=env_path,
        live_validator=lambda values: "rejected",
    )

    assert result.status == "live_auth_failed"
    assert not env_path.exists()


def test_import_web_auth_rejected_keeps_previous_env_untouched(tmp_path, monkeypatch) -> None:
    _clear_auth_env(monkeypatch)
    env_path = tmp_path / ".env"
    previous = (
        "PLAUD_AUTHORIZATION='Bearer old.token'\n"
        "PLAUD_X_DEVICE_ID='old-device'\n"
        "PLAUD_X_PLD_USER='old-user'\n"
    )
    env_path.write_text(previous, encoding="utf-8")

    result = import_web_auth(
        WebAuthCapture.model_validate_json(
            json.dumps(
                {
                    "authorization": "Bearer rejected.token",
                    "x_device_id": "new-device",
                    "x_pld_user": "new-user",
                    "cookie": "session=new",
                }
            )
        ),
        env_path=env_path,
        live_validator=lambda values: "rejected",
    )

    assert result.status == "live_auth_failed"
    # Validate-before-write: the previous .env is byte-identical, no rollback needed.
    assert env_path.read_bytes() == previous.encode("utf-8")


def test_import_web_auth_unreachable_writes_env_with_warning(tmp_path, monkeypatch) -> None:
    _clear_auth_env(monkeypatch)
    env_path = tmp_path / ".env"

    result = import_web_auth(
        {
            "authorization": "Bearer unverified.token.value",
            "x_device_id": "device-from-webkit",
            "x_pld_user": "user-from-webkit",
        },
        env_path=env_path,
        live_validator=lambda values: "unreachable",
    )

    assert result.status == "live_check_unavailable"
    assert "could not be verified" in result.detail
    assert "PLAUD_AUTHORIZATION='Bearer unverified.token.value'" in env_path.read_text(
        encoding="utf-8"
    )


def test_import_web_auth_expired_capture_skips_validator_and_write(tmp_path) -> None:
    env_path = tmp_path / ".env"
    expired_jwt = _make_jwt({"exp": int(time.time()) - 10, "iat": int(time.time()) - 90_000})

    def explode(values) -> str:
        raise AssertionError("live validator must not run for an already-expired capture")

    result = import_web_auth(
        {
            "authorization": f"Bearer {expired_jwt}",
            "x_device_id": "device-from-webkit",
            "x_pld_user": "user-from-webkit",
        },
        env_path=env_path,
        live_validator=explode,
    )

    assert result.status == "live_auth_failed"
    assert "expired" in result.detail
    assert result.cookie_captured is False
    assert not env_path.exists()


def test_default_live_validator_probes_candidate_credentials(tmp_path, monkeypatch) -> None:
    _clear_auth_env(monkeypatch)
    env_path = tmp_path / ".env"
    record: dict = {}
    monkeypatch.setattr(web_auth_mod, "PlaudClient", _fake_client(record))

    result = import_web_auth(
        {
            "authorization": "Bearer candidate.token.value",
            "x_device_id": "device-from-webkit",
            "x_pld_user": "user-from-webkit",
        },
        env_path=env_path,
    )

    assert result.status == "ok"
    # The probe must see the candidate credentials in memory — never .env.
    assert record["authorization"] == "Bearer candidate.token.value"
    assert record["timeout"] == 10.0
    assert record["limit"] == 1


def test_default_live_validator_maps_401_to_rejected(tmp_path, monkeypatch) -> None:
    _clear_auth_env(monkeypatch)
    env_path = tmp_path / ".env"
    record: dict = {}
    error = PlaudAPIError("Plaud HTTP 401 for GET /file/simple/web", status_code=401)
    monkeypatch.setattr(web_auth_mod, "PlaudClient", _fake_client(record, error=error))

    result = import_web_auth(
        {
            "authorization": "Bearer candidate.token.value",
            "x_device_id": "device-from-webkit",
            "x_pld_user": "user-from-webkit",
        },
        env_path=env_path,
    )

    assert result.status == "live_auth_failed"
    assert not env_path.exists()


def test_default_live_validator_maps_network_error_to_unreachable(tmp_path, monkeypatch) -> None:
    _clear_auth_env(monkeypatch)
    env_path = tmp_path / ".env"
    record: dict = {}
    error = PlaudAPIError("Plaud network error: connection refused")  # no status_code
    monkeypatch.setattr(web_auth_mod, "PlaudClient", _fake_client(record, error=error))

    result = import_web_auth(
        {
            "authorization": "Bearer candidate.token.value",
            "x_device_id": "device-from-webkit",
            "x_pld_user": "user-from-webkit",
        },
        env_path=env_path,
    )

    assert result.status == "live_check_unavailable"
    assert env_path.exists()


def test_import_web_auth_reports_write_failure_with_cookie_flag(tmp_path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    result = import_web_auth(
        {
            "authorization": "Bearer header.only",
            "x_device_id": "device-from-webkit",
            "x_pld_user": "user-from-webkit",
        },
        env_path=blocker / ".env",
        live_validator=lambda values: "ok",
    )

    assert result.status == "write_failed"
    assert result.cookie_captured is False  # header-only capture had no cookie


def test_web_auth_cli_reads_structured_json_from_stdin(tmp_path, monkeypatch) -> None:
    _clear_auth_env(monkeypatch)
    env_path = tmp_path / ".env"
    monkeypatch.setenv("PLAUD_ENV_FILE", str(env_path))
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["web-auth", "--json", "--stdin", "--skip-live"],
        input=json.dumps(
            {
                "authorization": "Bearer cli.token",
                "x_device_id": "cli-device",
                "x_pld_user": "cli-user",
                "cookie": "session=cli",
            }
        ),
    )

    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert body["status"] == "ok"
    assert "PLAUD_AUTHORIZATION='Bearer cli.token'" in env_path.read_text(encoding="utf-8")


def test_web_auth_cli_json_requires_stdin_flag() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["web-auth", "--json"])

    assert result.exit_code == 0  # JSON mode always exits 0
    assert json.loads(result.stdout)["status"] == "stdin_required"


def test_web_auth_cli_json_reports_invalid_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PLAUD_ENV_FILE", str(tmp_path / ".env"))
    runner = CliRunner()

    result = runner.invoke(app, ["web-auth", "--json", "--stdin"], input="not json")

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "invalid_payload"


def test_web_auth_cli_non_json_invalid_payload_exits_1(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PLAUD_ENV_FILE", str(tmp_path / ".env"))
    runner = CliRunner()

    result = runner.invoke(app, ["web-auth", "--stdin"], input="not json")

    assert result.exit_code == 1


def test_import_web_auth_arms_headless_refresh_from_workspace_list(tmp_path, monkeypatch) -> None:
    _clear_auth_env(monkeypatch)
    env_path = tmp_path / ".env"
    auth_jwt = _make_jwt({"exp": int(time.time()) + 86_400, "wid": "ws_abc"})

    result = import_web_auth(
        {
            "authorization": f"Bearer {auth_jwt}",
            "x_device_id": "device-from-webkit",
            "x_pld_user": "user-from-webkit",
            "workspace_list": json.dumps(
                [{"workspaceId": "ws_abc", "refreshToken": "ls-refresh-token"}]
            ),
        },
        env_path=env_path,
        live_validator=lambda values: "ok",
    )

    assert result.status == "ok"
    assert result.auto_refresh_armed is True
    assert "automatic renewal captured" in result.detail
    from core.config import read_env_file

    values = read_env_file(env_path)
    assert values["PLAUD_WS_REFRESH_TOKEN"] == "ls-refresh-token"
    assert values["PLAUD_WORKSPACE_ID"] == "ws_abc"


def test_import_web_auth_without_workspace_list_keeps_existing_bootstrap(
    tmp_path, monkeypatch
) -> None:
    _clear_auth_env(monkeypatch)
    env_path = tmp_path / ".env"
    from core.config import read_env_file, write_env_file

    auth_jwt = _make_jwt({"exp": int(time.time()) + 86_400, "wid": "ws_abc"})
    write_env_file(
        {
            "PLAUD_AUTHORIZATION": "bearer old",
            "PLAUD_X_DEVICE_ID": "old-dev",
            "PLAUD_X_PLD_USER": "old-user",
            "PLAUD_WORKSPACE_ID": "ws_abc",
            "PLAUD_WS_REFRESH_TOKEN": "existing-refresh",
        },
        env_path,
    )

    result = import_web_auth(
        {
            "authorization": f"Bearer {auth_jwt}",
            "x_device_id": "device-from-webkit",
            "x_pld_user": "user-from-webkit",
        },
        env_path=env_path,
        live_validator=lambda values: "ok",
    )

    assert result.status == "ok"
    assert result.auto_refresh_armed is True  # preserved counts as armed
    values = read_env_file(env_path)
    assert values["PLAUD_WS_REFRESH_TOKEN"] == "existing-refresh"


def test_rejected_existing_refresh_yields_to_fresh_web_login_candidate(
    tmp_path, monkeypatch
) -> None:
    """Regression: a dead Keychain token used to shadow the recovered pair."""
    _clear_auth_env(monkeypatch)
    env_path = tmp_path / ".env"
    from core.config import read_env_file, write_env_file

    now = int(time.time())
    old_jwt = _make_jwt({"iat": now - 3600, "exp": now + 3600, "wid": "ws_abc"})
    recovered_jwt = _make_jwt({"iat": now, "exp": now + 86_400, "wid": "ws_abc"})
    write_env_file(
        {
            "PLAUD_AUTHORIZATION": f"bearer {old_jwt}",
            "PLAUD_X_DEVICE_ID": "old-dev",
            "PLAUD_WORKSPACE_ID": "ws_abc",
            "PLAUD_WS_REFRESH_TOKEN": "dead-keychain-token",
        },
        env_path,
    )
    auth_status_mod.record_auth_rejection(status=-420, now=now - 1)

    result = import_web_auth(
        {
            "authorization": f"Bearer {recovered_jwt}",
            "x_device_id": "device-from-webkit",
            "workspace_list": json.dumps(
                [{"workspaceId": "ws_abc", "refreshToken": "fresh-browser-token"}]
            ),
        },
        env_path=env_path,
        live_validator=lambda values: "ok",
    )

    assert result.status == "ok"
    assert result.auto_refresh_armed is True
    assert read_env_file(env_path)["PLAUD_WS_REFRESH_TOKEN"] == "fresh-browser-token"


def test_import_web_auth_does_not_arm_missing_browser_refresh_token(tmp_path, monkeypatch) -> None:
    _clear_auth_env(monkeypatch)
    env_path = tmp_path / ".env"
    auth_jwt = _make_jwt({"exp": int(time.time()) + 86_400, "wid": "ws_abc"})
    from core.config import read_env_file

    result = import_web_auth(
        {
            "authorization": f"Bearer {auth_jwt}",
            "x_device_id": "device-from-webkit",
            "workspace_list": json.dumps([{"workspaceId": "ws_abc"}]),
        },
        env_path=env_path,
        live_validator=lambda values: "ok",
    )

    assert result.status == "ok"
    assert result.auto_refresh_armed is False
    values = read_env_file(env_path)
    assert "PLAUD_WS_REFRESH_TOKEN" not in values
    assert values["PLAUD_AUTHORIZATION"].startswith("Bearer ")
