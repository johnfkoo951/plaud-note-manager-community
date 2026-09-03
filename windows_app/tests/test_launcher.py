from __future__ import annotations

import inspect
import site
import sys
import tomllib
from pathlib import Path

from windows_app import __version__
from windows_app import launcher


def test_configure_environment_replaces_inherited_plaud_state(tmp_path):
    local = tmp_path / "LocalAppData"
    resources = tmp_path / "resources"
    resources.mkdir()
    env = {
        "LOCALAPPDATA": str(local),
        "PLAUD_AUTHORIZATION": "private-token-must-go",
        "PLAUD_DATA_DIR": "C:/private/data",
        "PLAUD_RESOURCE_ROOT": "C:/private/source",
        "PYTHONHOME": "C:/private/python",
        "PYTHONPATH": "C:/private/modules",
        "UNRELATED": "kept",
    }

    paths = launcher.configure_environment(
        local_app_data=local,
        resource_root=resources,
        environ=env,
    )

    assert "PLAUD_AUTHORIZATION" not in env
    assert env["UNRELATED"] == "kept"
    assert env["PLAUD_DATA_DIR"] == str(paths.data_dir)
    assert env["PLAUD_ENV_FILE"] == str(paths.env_file)
    assert env["PLAUD_RESOURCE_ROOT"] == str(resources.resolve())
    assert env["PLAUD_AUTO_REFRESH"] == "0"
    assert env["PLAUD_KEYCHAIN_SERVICE"] == launcher.KEYCHAIN_SERVICE
    assert env["PLAUD_AUTH_BLOB_FILE"] == str(paths.config_dir / "auth.bin")
    assert "PYTHONHOME" not in env
    assert "PYTHONPATH" not in env
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTHONSAFEPATH"] == "1"
    assert sys.dont_write_bytecode is True
    assert site.ENABLE_USER_SITE is False
    assert launcher.runtime_imports_are_isolated() is True
    assert str(paths.app_root).startswith(str(local.resolve()))
    assert paths.data_dir.is_dir()
    assert paths.config_dir.is_dir()
    assert paths.export_dir.is_dir()


def test_launcher_configures_before_importing_server():
    source = inspect.getsource(launcher.main)
    assert source.index("configure_environment()") < source.index("from .server import run_server")
    assert "from core" not in inspect.getsource(launcher)
    assert "import core" not in inspect.getsource(launcher)


def test_launcher_configures_before_importing_self_test():
    source = inspect.getsource(launcher.main)
    assert source.index("configure_environment()") < source.index(
        "from .self_test import run_self_test"
    )


def test_windows_package_version_matches_project_version():
    project_file = Path(__file__).parents[2] / "pyproject.toml"
    with project_file.open("rb") as handle:
        project_version = tomllib.load(handle)["project"]["version"]

    assert __version__ == project_version
