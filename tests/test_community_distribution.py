from __future__ import annotations

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
    assert "Unavailable in the Community edition" in result.stdout


def test_community_edition_keeps_local_paths_command_available() -> None:
    result = CliRunner().invoke(app, ["paths"])

    assert result.exit_code == 0
    assert "data" in result.stdout
    assert "templates" in result.stdout
