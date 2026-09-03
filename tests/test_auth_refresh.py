import json
import time

import httpx
import pytest
from typer.testing import CliRunner

import core.auth_status as auth_status_mod
import core.client as client_mod
import core.refresh_auth as refresh_mod
from cli.main import app
from core.config import load_config
from core.refresh_auth import refresh_auth
from core.secret_store import load_credential_values, update_credential_values
from tests.test_auth_status import _make_jwt

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

WINDOWS_CMD_CURL = f"""
curl ^"https://api-us1.plaud.ai/file/simple/web?limit=1^" ^
  -H ^"{AUTHORIZATION_HEADER}: Bearer windows.test.token^" ^
  -H ^"x-device-id: windows-device^" ^
  -b ^"session=windows-cookie; preference=ko^"
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


def test_refresh_auth_accepts_chrome_windows_cmd_curl(tmp_path, monkeypatch) -> None:
    for key in ("PLAUD_AUTHORIZATION", "PLAUD_X_DEVICE_ID", "PLAUD_COOKIE"):
        monkeypatch.delenv(key, raising=False)

    env_path = tmp_path / ".env"
    result = refresh_auth(env_path=env_path, curl_text=WINDOWS_CMD_CURL)

    assert result.status == "ok"
    assert result.cookie_captured is True
    cfg = load_config(env_path)
    assert cfg.base_url == "https://api-us1.plaud.ai"
    assert cfg.headers()["authorization"] == "Bearer windows.test.token"
    assert cfg.headers()["x-device-id"] == "windows-device"
    assert cfg.headers()["cookie"] == "session=windows-cookie; preference=ko"


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
        "https://apifoo.plaud.ai/filetag/",
        "https://api.plaud.ai.attacker.example/filetag/",
        "https://api.plaud.ai@attacker.example/filetag/",
        "https://user@api-apne1.plaud.ai/filetag/",
        "https://api-apne1.plaud.ai:8443/filetag/",
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


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (f"{AUTHORIZATION_HEADER}:   ", "missing required headers"),
        (f"{AUTHORIZATION_HEADER}: Basic dXNlcjpwYXNz", "non-empty Bearer token"),
        ("x-device-id:   ", "missing required headers"),
    ],
)
def test_refresh_auth_rejects_blank_or_non_bearer_required_headers(
    header: str, expected: str, tmp_path
) -> None:
    authorization = (
        f"{AUTHORIZATION_HEADER}: Bearer safe.test.token"
        if header.startswith("x-device-id")
        else header
    )
    device = "x-device-id: device-123" if not header.startswith("x-device-id") else header
    curl = f"curl 'https://api-apne1.plaud.ai/filetag/' -H '{authorization}' -H '{device}'"
    env_path = tmp_path / ".env"

    result = refresh_auth(env_path=env_path, curl_text=curl)

    assert result.status == "invalid_curl"
    assert expected in result.detail
    assert not env_path.exists()


def test_refresh_auth_rejects_oversized_or_nul_input_without_writing(tmp_path) -> None:
    env_path = tmp_path / ".env"

    oversized = refresh_auth(env_path=env_path, curl_text="x" * (256 * 1024 + 1))
    invalid_character = refresh_auth(env_path=env_path, curl_text=VALID_CURL + "\0")

    assert oversized.status == "invalid_curl"
    assert "unexpectedly large" in oversized.detail
    assert invalid_character.status == "invalid_curl"
    assert "NUL" in invalid_character.detail
    assert not env_path.exists()


def test_refresh_auth_rejects_newline_inside_header_without_echoing_value(tmp_path) -> None:
    env_path = tmp_path / ".env"
    curl = (
        "curl 'https://api-apne1.plaud.ai/filetag/' "
        f"-H '{AUTHORIZATION_HEADER}: Bearer safe-part\nsecret-part' "
        "-H 'x-device-id: device-123'"
    )

    result = refresh_auth(env_path=env_path, curl_text=curl)

    assert result.status == "invalid_curl"
    assert "control character" in result.detail
    assert "secret-part" not in result.detail
    assert not env_path.exists()


@pytest.mark.parametrize(
    "unsafe_fragment",
    [
        "-H 'authorization: Bearer safe\tsecret-marker'",
        "-H 'x-device-id: device\x01secret-marker'",
        "-H 'timezone: Asia/Seoul\rsecret-marker'",
        "-b 'session=safe\nsecret-marker'",
        "--cookie='session=safe\x7fsecret-marker'",
    ],
)
def test_refresh_auth_rejects_c0_or_del_in_every_credential_route(
    unsafe_fragment: str, tmp_path
) -> None:
    env_path = tmp_path / ".env"
    curl = VALID_CURL + "\n" + unsafe_fragment

    result = refresh_auth(env_path=env_path, curl_text=curl)

    assert result.status == "invalid_curl"
    assert "control character" in result.detail
    assert "secret-marker" not in result.detail
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


def test_refresh_auth_live_unreachable_keeps_previous_env_byte_identical(tmp_path) -> None:
    env_path = tmp_path / ".env"
    previous = "PLAUD_AUTHORIZATION='Bearer old.token'\nPLAUD_X_DEVICE_ID='old-device'\n"
    env_path.write_text(previous, encoding="utf-8")

    result = refresh_auth(
        env_path=env_path,
        curl_text=CURRENT_WEB_CURL,
        validate_live=True,
        live_validator=lambda values: "unreachable",
    )

    assert result.status == "live_check_unavailable"
    assert "header.payload.signature" not in result.detail
    assert "unchanged" in result.detail
    assert env_path.read_text(encoding="utf-8") == previous


@pytest.mark.parametrize(
    ("probe_kind", "expected_status"),
    [
        ("http_rejected", "live_auth_failed"),
        ("business_rejected", "live_auth_failed"),
        ("unreachable", "live_check_unavailable"),
    ],
)
@pytest.mark.parametrize("preexisting_memo", [False, True], ids=["absent", "existing"])
def test_default_curl_candidate_probe_never_mutates_active_rejection_memo(
    probe_kind: str,
    expected_status: str,
    preexisting_memo: bool,
    tmp_path,
    monkeypatch,
) -> None:
    env_path = tmp_path / ".env"
    previous_credentials = (
        "PLAUD_AUTHORIZATION='Bearer old.token'\nPLAUD_X_DEVICE_ID='old-device'\n"
    )
    env_path.write_text(previous_credentials, encoding="utf-8")

    memo_path = auth_status_mod.REJECTION_FILE
    original_memo = b'{"rejected_at":123,"status":-419}\n'
    if preexisting_memo:
        memo_path.write_bytes(original_memo)

    real_httpx_client = client_mod.httpx.Client

    def respond(request: httpx.Request) -> httpx.Response:
        if probe_kind == "http_rejected":
            return httpx.Response(401, request=request)
        if probe_kind == "business_rejected":
            return httpx.Response(
                200,
                request=request,
                json={"status": -419, "msg": "candidate expired"},
            )
        raise httpx.ConnectError("candidate probe offline", request=request)

    def mock_client(**kwargs):
        return real_httpx_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(client_mod.httpx, "Client", mock_client)
    result = refresh_auth(
        env_path=env_path,
        curl_text=CURRENT_WEB_CURL,
        validate_live=True,
    )

    assert result.status == expected_status
    assert env_path.read_text(encoding="utf-8") == previous_credentials
    if preexisting_memo:
        assert memo_path.read_bytes() == original_memo
    else:
        assert not memo_path.exists()


def test_curl_only_import_reports_that_automatic_renewal_is_not_armed(tmp_path) -> None:
    result = refresh_auth(env_path=tmp_path / ".env", curl_text=VALID_CURL)

    assert result.status == "ok"
    assert result.auto_refresh_armed is False
    assert "current access only" in result.detail
    assert "does not contain" in result.auto_refresh_detail


def test_curl_import_reports_preserved_same_workspace_refresh_as_armed(tmp_path) -> None:
    env_path = tmp_path / ".env"
    now = int(time.time())
    same_workspace_curl = VALID_CURL.replace(
        "test.token.value",
        _make_jwt({"exp": now + 86_400, "wid": "existing-workspace"}),
    )
    update_credential_values(
        {
            "PLAUD_WORKSPACE_ID": "existing-workspace",
            "PLAUD_WS_REFRESH_TOKEN": "existing-refresh-token",
            "PLAUD_WS_REFRESH_EXPIRES_AT": str(now + 30 * 86_400),
        },
        env_path,
    )

    result = refresh_auth(env_path=env_path, curl_text=same_workspace_curl)

    assert result.status == "ok"
    assert result.auto_refresh_armed is True
    assert "remains armed" in result.detail
    assert "preserved" in result.auto_refresh_detail
    assert load_credential_values(env_path)["PLAUD_WS_REFRESH_TOKEN"] == ("existing-refresh-token")


@pytest.mark.parametrize(
    "binding_case",
    ["opaque_candidate", "workspace_mismatch", "expired_refresh", "missing_horizon"],
)
def test_curl_import_clears_unprovable_or_expired_refresh_binding(
    binding_case: str, tmp_path
) -> None:
    env_path = tmp_path / ".env"
    now = int(time.time())
    candidate_token = _make_jwt({"exp": now + 86_400, "wid": "existing-workspace"})
    refresh_expiry: str | None = str(now + 30 * 86_400)
    if binding_case == "opaque_candidate":
        candidate_token = "opaque.access.token"
    elif binding_case == "workspace_mismatch":
        candidate_token = _make_jwt({"exp": now + 86_400, "wid": "other-workspace"})
    elif binding_case == "expired_refresh":
        refresh_expiry = str(now - 1)
    else:
        refresh_expiry = None

    stored: dict[str, str | None] = {
        "PLAUD_WORKSPACE_ID": "existing-workspace",
        "PLAUD_WS_REFRESH_TOKEN": "must-not-survive",
        "PLAUD_WS_REFRESH_EXPIRES_AT": refresh_expiry,
    }
    update_credential_values(stored, env_path)
    candidate_curl = VALID_CURL.replace("test.token.value", candidate_token)

    result = refresh_auth(env_path=env_path, curl_text=candidate_curl)

    assert result.status == "ok"
    assert result.auto_refresh_armed is False
    values = load_credential_values(env_path)
    assert "PLAUD_WORKSPACE_ID" not in values
    assert "PLAUD_WS_REFRESH_TOKEN" not in values
    assert "PLAUD_WS_REFRESH_EXPIRES_AT" not in values


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
    assert set(body) == {
        "status",
        "detail",
        "cookie_captured",
        "auto_refresh_armed",
        "auto_refresh_detail",
    }
    assert body["status"] == "ok"
    assert body["auto_refresh_armed"] is False
    assert "test.token.value" not in result.stdout  # bearer token stays off stdout
    assert env_path.exists()


def test_refresh_auth_cli_curl_only_output_does_not_promise_permanent_auth(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PLAUD_ENV_FILE", str(tmp_path / ".env"))

    result = CliRunner().invoke(app, ["refresh-auth", "--stdin"], input=VALID_CURL)

    assert result.exit_code == 0
    assert "automatic renewal: not armed" in result.stdout
    assert "current access only" in result.stdout
    assert "test.token.value" not in result.stdout


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
