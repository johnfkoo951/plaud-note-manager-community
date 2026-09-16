from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core import official_cli


def make_install(prefix: Path, version: str = "0.3.11", name: str = "@plaud-ai/cli") -> Path:
    package = prefix / "lib/node_modules/@plaud-ai/cli"
    (package / "dist").mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": name, "version": version, "bin": {"plaud": "dist/index.js"}})
    )
    (package / "dist/index.js").write_text("// test fixture, never executed\n")
    node = prefix / "bin/node"
    node.parent.mkdir(parents=True)
    node.write_text("// test fixture, never executed\n")
    node.chmod(0o700)
    return package


@pytest.fixture
def install(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(official_cli.Path, "home", lambda: home)
    package = make_install(home / ".nvm/versions/node/v22.22.1")
    monkeypatch.setenv("PLAUD_OFFICIAL_CLI_PACKAGE", str(package))
    monkeypatch.delenv("PLAUD_OFFICIAL_NODE", raising=False)
    return package


def test_discovery_ignores_project_plaud_in_path(install, monkeypatch, tmp_path):
    fake = tmp_path / "plaud"
    fake.write_text("unexpected project CLI")
    monkeypatch.setenv("PATH", str(tmp_path))
    found = official_cli.discover()
    assert found.package_path == install
    assert found.node_path == install.parents[3] / "bin/node"
    assert found.entrypoint == install / "dist/index.js"


def test_discovery_uses_numeric_nvm_version_order(install, monkeypatch):
    monkeypatch.delenv("PLAUD_OFFICIAL_CLI_PACKAGE")
    base = Path.home() / ".nvm/versions/node"
    make_install(base / "v9.99.0", "0.3.9")
    newest = make_install(base / "v24.2.0", "0.3.12")
    assert official_cli.discover().package_path == newest


@pytest.mark.parametrize(
    "defect", ["wrong_name", "missing_node", "outside_entry", "relative_override"]
)
def test_invalid_explicit_package_never_falls_back(install, monkeypatch, defect):
    if defect == "wrong_name":
        metadata = json.loads((install / "package.json").read_text())
        metadata["name"] = "plaud-note-manager"
        (install / "package.json").write_text(json.dumps(metadata))
    elif defect == "missing_node":
        (install.parents[3] / "bin/node").unlink()
    elif defect == "outside_entry":
        metadata = json.loads((install / "package.json").read_text())
        metadata["bin"]["plaud"] = "../outside.js"
        (install / "package.json").write_text(json.dumps(metadata))
        (install.parent / "outside.js").write_text("unsafe")
    else:
        monkeypatch.setenv("PLAUD_OFFICIAL_CLI_PACKAGE", "relative/package")
    with pytest.raises(official_cli.OfficialCLIError) as exc:
        official_cli.discover()
    assert exc.value.code == "invalid_installation"


def test_offline_status_only_stats_token_file(install, monkeypatch):
    token = Path.home() / ".plaud/tokens.json"
    token.parent.mkdir()
    token.write_text("THIS MUST NEVER BE READ")
    real_read = Path.read_text

    def guarded_read(path, *args, **kwargs):
        assert path != token
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    result = official_cli.status()
    assert result["installed"] and result["token_file_exists"]
    assert result["auth_state"] == "not_checked"
    assert not result["live_checked"]
    assert "THIS MUST" not in json.dumps(result)


def test_status_missing_installation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        official_cli, "_candidate_packages", lambda: [(tmp_path, tmp_path / "node")]
    )
    monkeypatch.delenv("PLAUD_OFFICIAL_CLI_PACKAGE", raising=False)
    result = official_cli.status()
    assert result["installed"] is False and result["capabilities"] == []


def test_live_status_discards_account_output_and_isolates_runtime(install, monkeypatch):
    monkeypatch.setenv("PLAUD_AUTHORIZATION", "private-web-token")
    monkeypatch.setenv("PLAUD_WS_REFRESH_TOKEN", "private-refresh-token")
    monkeypatch.setenv("DOTENV_CONFIG_PATH", "/sensitive/project/.env")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/unrelated/injected.js")

    def run(argv, **kwargs):
        assert argv == [str(install.parents[3] / "bin/node"), str(install / "dist/index.js"), "me"]
        assert kwargs["cwd"] == install and kwargs["timeout"] == 7
        assert kwargs.get("shell", False) is False
        assert kwargs["env"]["DOTENV_CONFIG_PATH"] == "/dev/null"
        assert kwargs["env"]["PLAUD_TELEMETRY_DISABLED"] == "1"
        assert "NODE_OPTIONS" not in kwargs["env"]
        assert "PLAUD_AUTHORIZATION" not in kwargs["env"]
        assert "PLAUD_WS_REFRESH_TOKEN" not in kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "private account email", "private debug")

    monkeypatch.setattr(official_cli.subprocess, "run", run)
    result = official_cli.status(live=True, timeout=7)
    assert result["auth_state"] == "valid" and result["live_checked"]
    assert "private account" not in json.dumps(result)
    assert "private debug" not in json.dumps(result)


@pytest.mark.parametrize(
    "exit_code,expected",
    [(1, "command_failed"), (2, "needs_login"), (3, "network_error"), (4, "timeout")],
)
def test_failed_live_status_does_not_expose_stderr(install, monkeypatch, exit_code, expected):
    monkeypatch.setattr(
        official_cli.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, exit_code, "secret", "Bearer secret"),
    )
    result = official_cli.status(live=True)
    assert result["auth_state"] == expected
    assert "secret" not in json.dumps(result)


def test_summary_preserves_all_note_metadata_without_links(install, monkeypatch):
    notes = [
        {
            "data_type": "auto_sum_note",
            "data_title": "첫 요약",
            "data_tab_name": "탭 1",
            "data_content": "본문 A",
            "data_link": "https://signed.example/?secret",
        },
        {
            "data_type": "consumer_note",
            "data_title": "편집",
            "data_tab_name": "탭 2",
            "data_content": "본문 B",
        },
    ]

    def run(argv, **kwargs):
        assert argv[-3:] == ["summary", "abc-123", "--json"]
        return subprocess.CompletedProcess(argv, 0, json.dumps(notes), "")

    monkeypatch.setattr(official_cli.subprocess, "run", run)
    result = official_cli.read_recording("abc-123")
    assert result["complete"] and result["available"]
    assert result["text"] == "본문 A\n\n본문 B"
    assert result["notes"][1]["type"] == "consumer_note"
    assert result["notes"][0]["title"] == "첫 요약"
    assert result["notes"][0]["tab_name"] == "탭 1"
    assert result["notes"][0]["linked"]
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "notes",
    [[], [{"data_type": "auto_sum_note", "data_link": "signed-url"}], [{"data_content": []}]],
)
def test_absent_or_link_only_summary_never_claims_cached_content(install, monkeypatch, notes):
    monkeypatch.setattr(
        official_cli.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, json.dumps(notes), ""),
    )
    result = official_cli.read_recording("abc123")
    assert not result["available"] and not result["complete"]
    assert "signed-url" not in json.dumps(result)


@pytest.mark.parametrize("payload", ["not json", '{"data": []}', '["unexpected"]'])
def test_summary_schema_errors_are_safe(install, monkeypatch, payload):
    monkeypatch.setattr(
        official_cli.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, payload, ""),
    )
    with pytest.raises(official_cli.OfficialCLIError) as exc:
        official_cli.read_recording("abc123")
    assert exc.value.code == "invalid_output"


def test_transcript_reads_only_export_file_and_removes_temp(install, monkeypatch):
    export_paths = []

    def run(argv, **kwargs):
        assert argv[2:6] == ["transcript", "abc123", "--block", "transaction_polish"]
        export = Path(argv[argv.index("--output") + 1])
        export_paths.append(export)
        export.write_text("[00:01] Speaker: 실제 본문\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "Transcript saved to path", "Fetching...")

    monkeypatch.setattr(official_cli.subprocess, "run", run)
    result = official_cli.read_recording("abc123", "transcript", block="transaction_polish")
    assert result["text"] == "[00:01] Speaker: 실제 본문\n"
    assert result["complete"] and not export_paths[0].exists()


def test_success_exit_without_export_is_unavailable(install, monkeypatch):
    monkeypatch.setattr(
        official_cli.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, 'No "transaction" transcript for this recording.', ""
        ),
    )
    result = official_cli.read_recording("abc123", "transcript")
    assert not result["available"] and not result["complete"] and result["text"] == ""


def test_timeout_is_bounded_without_output_leak(install, monkeypatch):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output="sensitive")

    monkeypatch.setattr(official_cli.subprocess, "run", run)
    with pytest.raises(official_cli.OfficialCLIError) as exc:
        official_cli.read_recording("abc123", timeout=0.1)
    assert exc.value.code == "timeout" and "sensitive" not in str(exc.value)


@pytest.mark.parametrize(
    "options,code",
    [
        ({"file_id": "--help"}, "invalid_file_id"),
        ({"file_id": "../file"}, "invalid_file_id"),
        ({"file_id": "abc123", "kind": "delete"}, "invalid_kind"),
        ({"file_id": "abc123", "block": "--help"}, "invalid_block"),
        ({"file_id": "abc123", "timeout": float("inf")}, "invalid_timeout"),
    ],
)
def test_invalid_read_never_executes(install, options, code):
    with pytest.raises(official_cli.OfficialCLIError) as exc:
        official_cli.read_recording(**options)
    assert exc.value.code == code
