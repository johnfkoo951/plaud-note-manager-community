"""Privacy-first entry point for the Windows Community Lite application."""

from __future__ import annotations

import logging
import os
import site
import sys
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

APP_DIRECTORY = "PlaudNoteManagerCommunityLite"
APP_SUPPORT_ID = "com.cmdspace.PlaudNoteManagerCommunity.WindowsLite"
KEYCHAIN_SERVICE = f"{APP_SUPPORT_ID}.auth"


@dataclass(frozen=True)
class RuntimePaths:
    app_root: Path
    data_dir: Path
    config_dir: Path
    export_dir: Path
    env_file: Path
    resource_root: Path


def _user_site_paths() -> set[Path]:
    configured = site.getusersitepackages()
    values = [configured] if isinstance(configured, str) else configured
    paths: set[Path] = set()
    for value in values:
        try:
            paths.add(Path(value).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
    return paths


def _isolate_runtime_imports() -> None:
    """Apply isolation flags that environment variables cannot set retroactively."""

    sys.dont_write_bytecode = True
    site.ENABLE_USER_SITE = False
    user_sites = _user_site_paths()
    safe_path: list[str] = []
    for entry in sys.path:
        try:
            resolved = Path(entry).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if resolved not in user_sites:
            safe_path.append(entry)
    sys.path[:] = safe_path


def runtime_imports_are_isolated() -> bool:
    """Return whether late imports are insulated from the per-user site directory."""

    if not sys.dont_write_bytecode or site.ENABLE_USER_SITE is not False:
        return False
    user_sites = _user_site_paths()
    for entry in sys.path:
        try:
            if Path(entry).expanduser().resolve() in user_sites:
                return False
        except (OSError, RuntimeError):
            return False
    return True


def _default_resource_root() -> Path:
    """Resolve packaged resources without consulting an inherited Plaud path."""

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        root = Path(frozen_root)
        runtime = root / "runtime"
        return runtime if runtime.is_dir() else root
    if getattr(sys, "frozen", False):
        runtime = Path(sys.executable).resolve().parent / "resources"
        return runtime
    return Path(__file__).resolve().parent.parent


def configure_environment(
    *,
    local_app_data: Path | None = None,
    resource_root: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> RuntimePaths:
    """Set the complete Community namespace before any ``core`` import.

    Existing PLAUD_* values are discarded so a participant's shell, another
    checkout, or the private application cannot redirect this process to its
    credentials or data.  The Windows credential backend itself is supplied
    by ``core.secret_store`` and is intentionally imported only after this
    function returns.
    """

    env = os.environ if environ is None else environ
    # Shared core libraries must never emit recording ids, response bodies, or
    # credential errors through Python's fallback logging handler.
    logging.disable(logging.CRITICAL)
    for key in tuple(env):
        if key.startswith("PLAUD_"):
            env.pop(key, None)
    # These variables have already influenced interpreter startup, so clear
    # them for child processes and actively sanitize sys.path below.
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    _isolate_runtime_imports()

    if local_app_data is None:
        configured = env.get("LOCALAPPDATA")
        local_app_data = Path(configured) if configured else Path.home() / "AppData" / "Local"
    local_app_data = Path(local_app_data).expanduser().resolve()
    resource_root = Path(resource_root or _default_resource_root()).expanduser().resolve()

    app_root = local_app_data / "CMDSPACE" / APP_DIRECTORY
    data_dir = app_root / "data"
    config_dir = app_root / "config"
    export_dir = app_root / "exports"
    env_file = config_dir / "settings.env"

    for directory in (app_root, data_dir, config_dir, export_dir):
        directory.mkdir(parents=True, exist_ok=True)

    isolated = {
        "PLAUD_DISTRIBUTION_PROFILE": "community-windows-lite",
        "PLAUD_APP_SUPPORT_ID": APP_SUPPORT_ID,
        "PLAUD_KEYCHAIN_SERVICE": KEYCHAIN_SERVICE,
        "PLAUD_DATA_DIR": str(data_dir),
        "PLAUD_ENV_FILE": str(env_file),
        "PLAUD_AUTH_BLOB_FILE": str(config_dir / "auth.bin"),
        "PLAUD_RESOURCE_ROOT": str(resource_root),
        "PLAUD_TEMPLATES_DIR": str(resource_root / "templates"),
        "PLAUD_EXPORT_DIR": str(export_dir),
        # Never rotate or refresh credentials in the background. A cURL import
        # is the only authentication action exposed by Windows Community Lite.
        "PLAUD_AUTO_REFRESH": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
    }
    env.update(isolated)

    return RuntimePaths(
        app_root=app_root,
        data_dir=data_dir,
        config_dir=config_dir,
        export_dir=export_dir,
        env_file=env_file,
        resource_root=resource_root,
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    paths = configure_environment()

    try:
        if args == ["--self-test"]:
            # The packaged executable can verify its loopback boundary without
            # loading account data or making any Plaud Cloud request.
            from .self_test import run_self_test

            return run_self_test()
        if args:
            print("지원하지 않는 실행 옵션입니다.", file=sys.stderr)
            return 2

        # Security invariant: this import stays below configure_environment().
        from .server import run_server

        run_server(paths)
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001 - privacy boundary must redact every startup failure
        # Never emit request bodies, recording content, filesystem paths, or
        # credential diagnostics to a console/log file in the participant app.
        print("Plaud Community Lite를 시작하지 못했습니다.", file=sys.stderr)
        return 1
    return 0
