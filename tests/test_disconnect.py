from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from cli.main import app
from core.config import read_env_file, write_env_file


def test_disconnect_cli_scrubs_auth_and_preserves_local_library(
    tmp_path: Path, monkeypatch
) -> None:
    app_support = tmp_path / "Application Support" / "community"
    settings_env = app_support / "settings.env"
    database = app_support / "data" / "plaud.db"
    recording = app_support / "data" / "transcripts" / "recording-1" / "plaud.md"
    database.parent.mkdir(parents=True)
    recording.parent.mkdir(parents=True)
    database.write_bytes(b"database-sentinel")
    recording.write_text("recording-sentinel", encoding="utf-8")
    write_env_file(
        {
            "PLAUD_AUTHORIZATION": "legacy-placeholder",
            "PLAUD_X_DEVICE_ID": "device-placeholder",
            "PLAUD_AUTO_REFRESH": "1",
        },
        settings_env,
    )
    monkeypatch.setenv("PLAUD_ENV_FILE", str(settings_env))

    result = CliRunner().invoke(app, ["disconnect", "--json"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {
        "status": "ok",
        "credentials_removed": True,
        "local_data_preserved": True,
    }
    assert read_env_file(settings_env) == {"PLAUD_AUTO_REFRESH": "1"}
    assert database.read_bytes() == b"database-sentinel"
    assert recording.read_text(encoding="utf-8") == "recording-sentinel"
    assert "legacy-placeholder" not in result.stdout
