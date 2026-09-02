import json

import pytest
from typer.testing import CliRunner

import core.refresh_auth as refresh_mod
from cli.main import app
from core.config import load_config
from core.refresh_auth import refresh_auth

AUTHORIZATION_HEADER = "author" + "ization"

VALID_CURL = f"""
curl 'https://api-apne1.plaud.ai/filetag/' \\
  -H '{AUTHORIZATION_HEADER}: Bearer test.token.value' \\
  -H 'x-device-id: device-123' \\
  -H 'x-pld-user: user-1234567890123456' \\
  -H 'x-pld-tag: legacy-tag' \\
  -H 'cookie: sessionid=abc; workspace=cmds'
"""

CURRENT_WEB_CURL = f"""
curl 'https://api-apne1.plaud.ai/summary/community/templates/weekly_recommend' \\
  -H 'accept: application/json, text/plain, */*' \\
  -H 'app-language: en' \\
  -H 'app-platform: web' \\
  -H '{AUTHORIZATION_HEADER}: bearer header.payload.signature' \\
  -H 'content-type: application/json' \\
  -b 'session=abc; preference=ko' \\
  -H 'edit-from: web' \\
  -H 'origin: https://web.plaud.ai' \\
  -H 'timezone: Asia/Seoul' \\
  -H 'x-device-id: current-device' \\
  --data-raw '{{"language_os":"en"}}'
"""


def test_refresh_auth_keeps_curl_clipboard_concept_and_cookie(tmp_path, monkeypatch) -> None:
    for key in (
        "PLAUD_AUTHORIZATION",
        "PLAUD_X_DEVICE_ID",
        "PLAUD_X_PLD_USER",
        "PLAUD_X_PLD_TAG",
        "PLAUD_COOKIE",
    ):
        monkeypatch.delenv(key, raising=False)

    env_path = tmp_path / ".env"

    result = refresh_auth(env_path=env_path, curl_text=VALID_CURL)

    assert result.status == "ok"
    assert result.cookie_captured is True
    written = env_path.read_text(encoding="utf-8")
    assert "PLAUD_COOKIE='sessionid=abc; workspace=cmds'" in written

    cfg = load_config(env_path)
    assert cfg.headers()["cookie"] == "sessionid=abc; workspace=cmds"
    assert cfg.headers()["x-pld-tag"] == "legacy-tag"


def test_refresh_auth_accepts_current_weekly_curl_without_legacy_user(
    tmp_path, monkeypatch
) -> None:
    for key in (
        "PLAUD_AUTHORIZATION",
        "PLAUD_X_DEVICE_ID",
        "PLAUD_X_PLD_USER",
        "PLAUD_COOKIE",
        "PLAUD_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)

    env_path = tmp_path / ".env"
    result = refresh_auth(env_path=env_path, curl_text=CURRENT_WEB_CURL)

    assert result.status == "ok"
    assert result.cookie_captured is True
    cfg = load_config(env_path)
    assert cfg.base_url == "https://api-apne1.plaud.ai"
    assert cfg.headers()["authorization"] == "bearer header.payload.signature"
    assert cfg.headers()["cookie"] == "session=abc; preference=ko"
    assert "x-pld-user" not in cfg.headers()
    assert "PLAUD_X_PLD_USER" not in env_path.read_text(encoding="utf-8")


def test_refresh_auth_accepts_single_line_curl_and_region_host(tmp_path, monkeypatch) -> None:
    for key in ("PLAUD_AUTHORIZATION", "PLAUD_X_DEVICE_ID", "PLAUD_X_PLD_USER"):
        monkeypatch.delenv(key, raising=False)

    curl = (
        "curl 'https://api-eu1.plaud.ai/filetag/' "
        f"-H '{AUTHORIZATION_HEADER}: Bearer single.line.token' "
        "-H 'x-device-id: one-line-device'"
    )
    env_path = tmp_path / ".env"

    assert refresh_auth(env_path=env_path, curl_text=curl).status == "ok"
    cfg = load_config(env_path)
    assert cfg.base_url == "https://api-eu1.plaud.ai"
    assert "x-pld-user" not in cfg.headers()


def test_refresh_auth_rejects_non_plaud_target_without_writing(tmp_path) -> None:
    env_path = tmp_path / ".env"
    curl = (
        "curl 'https://example.com/' "
        f"-H '{AUTHORIZATION_HEADER}: Bearer should.not.persist' "
        "-H 'x-device-id: nope'"
    )

    result = refresh_auth(env_path=env_path, curl_text=curl)

    assert result.status == "invalid_curl"
    assert "api-*.plaud.ai" in result.detail
    assert not env_path.exists()


@pytest.mark.parametrize(
    "target",
    [
        "http://api-apne1.plaud.ai/filetag/",
        "https://api.plaud.ai.attacker.example/filetag/",
        "https://api.plaud.ai@attacker.example/filetag/",
    ],
)
def test_refresh_auth_rejects_malicious_or_ambiguous_api_origins(target: str, tmp_path) -> None:
    env_path = tmp_path / ".env"
    curl = (
        f"curl '{target}' "
        f"-H '{AUTHORIZATION_HEADER}: Bearer should.not.persist' "
        "-H 'x-device-id: untrusted-device'"
    )

    result = refresh_auth(env_path=env_path, curl_text=curl)

    assert result.status == "invalid_curl"
    assert not env_path.exists()


def test_refresh_auth_stays_quiet_for_json_callers(tmp_path, capsys) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("PLAUD_AUTHORIZATION='old'\n", encoding="utf-8")
    curl = f"""
curl 'https://api-apne1.plaud.ai/filetag/' \\
  -H '{AUTHORIZATION_HEADER}: Bearer new.token.value' \\
  -H 'x-device-id: device-123' \\
  -H 'x-pld-user: user-1234567890123456'
"""

    result = refresh_auth(env_path=env_path, curl_text=curl)

    assert result.status == "ok"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "PLAUD_AUTHORIZATION='Bearer new.token.value'" in env_path.read_text(encoding="utf-8")


def test_refresh_auth_reports_invalid_curl(tmp_path) -> None:
    env_path = tmp_path / ".env"

    result = refresh_auth(
        env_path=env_path,
        curl_text="curl 'https://api-apne1.plaud.ai/filetag/' -H 'foo: bar'",
    )

    assert result.status == "invalid_curl"
    assert "missing required headers" in result.detail
    assert not env_path.exists()


def test_refresh_auth_live_rejection_keeps_previous_env_byte_identical(tmp_path) -> None:
    env_path = tmp_path / ".env"
    previous = "PLAUD_AUTHORIZATION='Bearer old.token'\nPLAUD_X_DEVICE_ID='old-device'\n"
    env_path.write_text(previous, encoding="utf-8")

    result = refresh_auth(
        env_path=env_path,
        curl_text=CURRENT_WEB_CURL,
        validate_live=True,
        live_validator=lambda values: "rejected",
    )

    assert result.status == "live_auth_failed"
    assert "header.payload.signature" not in result.detail
    assert env_path.read_text(encoding="utf-8") == previous


def test_current_capture_removes_legacy_user_from_same_process(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PLAUD_AUTHORIZATION='Bearer old.token'\n"
        "PLAUD_X_DEVICE_ID='old-device'\n"
        "PLAUD_X_PLD_USER='legacy-user'\n",
        encoding="utf-8",
    )
    assert load_config(env_path).headers()["x-pld-user"] == "legacy-user"
    assert refresh_auth(env_path=env_path, curl_text=CURRENT_WEB_CURL).status == "ok"

    assert "x-pld-user" not in load_config(env_path).headers()


def test_refresh_auth_reports_empty_clipboard(tmp_path) -> None:
    result = refresh_auth(env_path=tmp_path / ".env", curl_text="   \n  ")

    assert result.status == "clipboard_empty"


def test_refresh_auth_reports_missing_pbpaste(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"

    def boom(*args, **kwargs):
        raise FileNotFoundError("pbpaste")

    monkeypatch.setattr(refresh_mod.subprocess, "run", boom)

    result = refresh_auth(env_path=env_path)  # curl_text=None → pasteboard path

    assert result.status == "pbpaste_missing"
    assert not env_path.exists()


def test_refresh_auth_honors_plaud_env_file(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / "custom.env"
    monkeypatch.setenv("PLAUD_ENV_FILE", str(env_path))

    result = refresh_auth(curl_text=VALID_CURL)

    assert result.status == "ok"
    assert "PLAUD_AUTHORIZATION='Bearer test.token.value'" in env_path.read_text(encoding="utf-8")


def test_refresh_auth_cli_json_stdin_never_prints_tokens(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    monkeypatch.setenv("PLAUD_ENV_FILE", str(env_path))
    runner = CliRunner()

    result = runner.invoke(app, ["refresh-auth", "--json", "--stdin"], input=VALID_CURL)

    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert set(body) == {"status", "detail", "cookie_captured"}
    assert body["status"] == "ok"
    assert "test.token.value" not in result.stdout  # bearer token stays off stdout
    assert env_path.exists()


def test_refresh_auth_cli_json_stdin_garbage_exits_0(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PLAUD_ENV_FILE", str(tmp_path / ".env"))
    runner = CliRunner()

    result = runner.invoke(app, ["refresh-auth", "--json", "--stdin"], input="not a curl")

    assert result.exit_code == 0  # JSON mode always exits 0
    assert json.loads(result.stdout)["status"] == "invalid_curl"


def test_refresh_auth_cli_empty_clipboard_exits_2(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PLAUD_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setattr(refresh_mod, "_read_pasteboard", lambda: "")
    runner = CliRunner()

    result = runner.invoke(app, ["refresh-auth"])

    assert result.exit_code == 2


def test_refresh_auth_cli_missing_pbpaste_exits_3(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PLAUD_ENV_FILE", str(tmp_path / ".env"))

    def boom() -> str:
        raise RuntimeError("pbpaste not found")

    monkeypatch.setattr(refresh_mod, "_read_pasteboard", boom)
    runner = CliRunner()

    result = runner.invoke(app, ["refresh-auth"])

    assert result.exit_code == 3
