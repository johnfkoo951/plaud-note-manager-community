from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli.main import app


@pytest.mark.parametrize(
    "argv",
    [
        ["vault-index"],
        ["metadata-generate", "sample-recording"],
        ["claude"],
    ],
)
def test_community_edition_blocks_private_or_external_commands(argv: list[str]) -> None:
    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 2
    # Handlers whose modules were deleted are gone entirely (typer reports
    # "No such command"); remaining private commands are refused by the guard.
    combined = result.stdout + (result.stderr or "") + str(result.output)
    assert "Unavailable in the Community edition" in combined or "No such command" in combined


def test_community_edition_keeps_local_paths_command_available() -> None:
    result = CliRunner().invoke(app, ["paths"])

    assert result.exit_code == 0
    assert "data" in result.stdout
    assert "templates" in result.stdout


@pytest.mark.parametrize(
    "command",
    ["auto-folder", "provider-key-status", "elevenlabs-transcribe"],
)
def test_community_edition_registers_opt_in_integration_commands(command: str) -> None:
    result = CliRunner().invoke(app, [command, "--help"])

    assert result.exit_code == 0, result.output


def test_macos_community_exposes_manual_curl_fallback() -> None:
    source = (
        Path(__file__).parents[1] / "app" / "Sources" / "PlaudNoteApp" / "PlaudAuthSheet.swift"
    ).read_text(encoding="utf-8")

    body = source.split("var body: some View", 1)[1].split("private var header", 1)[0]
    assert "browserImportCard" in body
    assert "advancedCurl" in body
    assert body.index("browserImportCard") < body.index("embeddedLoginFallback")
    assert "@State private var showEmbeddedLogin = false" in source
    assert "if !DistributionProfile.isCommunity {\n                browserImportCard" not in body
    assert "if !DistributionProfile.isCommunity {\n                autoRecoverCard" in body

    curl_handler = source.split("private func authenticateWithCurl", 1)[1]
    assert "runAutoRecover()" not in curl_handler
    assert "Durable automatic renewal is not armed" in curl_handler
