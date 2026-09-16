"""Consent-gated text-model runner for Community folder routing.

The runtime user must explicitly choose both a provider and a backend and pass
``confirmed_external=True`` for every preview run.  Until then this module does
not inspect provider credentials, launch a vendor CLI, or make a network call.
API keys come only from the OS-protected provider store.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Protocol

import httpx

from . import app_config

PROVIDER_LABELS: dict[str, str] = {
    "claude": "Anthropic",
    "codex": "OpenAI",
    "gemini": "Google",
    "grok": "xAI",
}
API_ONLY_PROVIDERS = frozenset({"gemini", "grok"})

SECRET_PROVIDER = {
    "claude": "anthropic",
    "codex": "openai",
    "gemini": "gemini",
    "grok": "grok",
}

CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "browser_use",
    "in_app_browser",
    "apps",
    "plugins",
    "hooks",
    "skill_search",
    "computer_use",
)
CODEX_PREFLIGHT_TIMEOUT_SECONDS = 15

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_WINDOWS_JOB_START_GATE = "PLAUD_WINDOWS_JOB_ASSIGNED_V1\n"

CLI_COMMANDS: dict[str, list[str]] = {
    "claude": [
        "claude",
        "--print",
        "--no-session-persistence",
        "--restricted",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--tools",
        "",
        "--max-turns",
        "1",
    ],
    "codex": [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        *(argument for feature in CODEX_DISABLED_FEATURES for argument in ("--disable", feature)),
        "-",
    ],
}

CLI_STRIP_ENV: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY",),
    "codex": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "grok": ("XAI_API_KEY",),
}

CLI_FALLBACK_PATHS: dict[str, tuple[Path, ...]] = {
    provider: (
        Path.home() / ".local" / "bin" / executable,
        Path.home() / ".npm-global" / "bin" / executable,
        Path("/opt/homebrew/bin") / executable,
        Path("/usr/local/bin") / executable,
    )
    for provider, executable in {
        "claude": "claude",
        "codex": "codex",
    }.items()
}


class ExternalConsentRequired(RuntimeError):
    """The user did not explicitly approve this external preview request."""


class ModelUnavailable(RuntimeError):
    """The selected provider route is not configured on this computer."""


class ModelFailed(RuntimeError):
    """The provider ran but did not return usable text."""


class _ConfiguredModel:
    """Sentinel distinguishing an omitted model id from a snapshotted None."""


_CONFIGURED_MODEL = _ConfiguredModel()


class _CliProcess(Protocol):
    """Small process surface shared by Popen and the Windows Job wrapper."""

    pid: int
    returncode: int | None

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]: ...

    def kill(self) -> None: ...


class _WindowsJob:
    """Own one Windows Job configured to kill every member when closed."""

    def __init__(self, handle: int, kernel32: Any) -> None:
        self._handle = handle
        self._kernel32 = kernel32

    def assign(self, pid: int) -> None:
        """Assign the gated launcher before it receives input or starts children."""

        import ctypes

        process = self._kernel32.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_SET_QUOTA,
            False,
            pid,
        )
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._kernel32.AssignProcessToJobObject(self._handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._kernel32.CloseHandle(process)

    def terminate(self) -> bool:
        """Terminate all members and release the final local Job handle."""

        if self._handle is None:
            return True
        terminated = bool(self._kernel32.TerminateJobObject(self._handle, 1))
        closed = self.close()
        # A successful close is itself a full-tree termination because this
        # Job was created with KILL_ON_JOB_CLOSE.
        return terminated or closed

    def close(self) -> bool:
        """Release the Job; KILL_ON_JOB_CLOSE removes detached descendants."""

        if self._handle is None:
            return True
        handle = self._handle
        self._handle = None
        return bool(self._kernel32.CloseHandle(handle))


class _WindowsJobProcess:
    """Popen-compatible provider launcher held inside a kill-on-close Job.

    Python's Popen does not expose its primary thread handle, so assigning a
    vendor process after it starts has a child-spawn race.  Instead, this starts
    our bundled helper with a pipe gate, assigns that blocked helper to the Job,
    and only then transmits stdin.  The helper launches the vendor, whose whole
    descendant tree automatically inherits the Job.
    """

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self._job = _create_windows_kill_on_close_job()
        self._tree_terminated = False
        self._communication_started = False
        helper = Path(__file__).with_name("_windows_job_runner.py")
        helper_argv = [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(helper),
            "--",
            *argv,
        ]
        popen_kwargs = dict(kwargs)
        # The private input is the launch gate.  Even DEVNULL callers must keep
        # this pipe open until after AssignProcessToJobObject succeeds.
        popen_kwargs["stdin"] = subprocess.PIPE
        popen_kwargs.setdefault("close_fds", True)
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        popen_kwargs["creationflags"] = popen_kwargs.get("creationflags", 0) | create_no_window
        try:
            process = subprocess.Popen(helper_argv, **popen_kwargs)
        except BaseException:
            self._job.close()
            raise
        self._process = process
        try:
            self._job.assign(process.pid)
        except BaseException:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            self._job.close()
            raise

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        if self._communication_started:
            gated_input = input
        else:
            gated_input = _WINDOWS_JOB_START_GATE + (input or "")
            self._communication_started = True
        try:
            result = self._process.communicate(input=gated_input, timeout=timeout)
        except BaseException:
            self._tree_terminated = self._job.terminate()
            raise
        # The vendor leader may have returned while a daemonized descendant is
        # still alive.  Terminate the Job and close its kill-on-close handle on
        # success too; never accept output if whole-tree cleanup is uncertain.
        self._tree_terminated = self._job.terminate()
        if not self._tree_terminated:
            raise ModelFailed("Windows provider process-tree cleanup failed")
        return result

    def terminate_tree(self) -> bool:
        if self._tree_terminated:
            return True
        self._tree_terminated = self._job.terminate()
        return self._tree_terminated

    def kill(self) -> None:
        self.terminate_tree()
        self._process.kill()


def _create_windows_kill_on_close_job() -> _WindowsJob:
    """Create a Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE."""

    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise ctypes.WinError(error)
    return _WindowsJob(handle, kernel32)


def _launch_cli_process(argv: list[str], **kwargs: Any) -> _CliProcess:
    if sys.platform == "win32":
        return _WindowsJobProcess(argv, **kwargs)
    return subprocess.Popen(argv, **kwargs)


def validate_route(provider: str, backend: str) -> tuple[str, str]:
    provider = provider.strip().casefold()
    backend = backend.strip().casefold()
    if provider not in PROVIDER_LABELS:
        raise ModelUnavailable(f"unknown model provider: {provider or '(empty)'}")
    if backend not in {"cli", "api"}:
        raise ModelUnavailable(f"backend must be cli or api, got: {backend or '(empty)'}")
    if backend == "cli" and provider in API_ONLY_PROVIDERS:
        raise ModelUnavailable(f"{provider} supports the api backend only")
    return provider, backend


def _api_key(provider: str) -> str | None:
    from .provider_secrets import get_api_key

    return get_api_key(SECRET_PROVIDER[provider])


def _has_api_key(provider: str) -> bool:
    from .provider_secrets import has_api_key

    return has_api_key(SECRET_PROVIDER[provider])


def _resolve_binary(provider: str) -> str | None:
    command = CLI_COMMANDS.get(provider)
    if not command:
        return None
    found = shutil.which(command[0])
    if found:
        return found
    candidates = list(CLI_FALLBACK_PATHS.get(provider, ()))
    if sys.platform == "win32":
        app_data = os.environ.get("APPDATA")
        if app_data:
            candidates.extend(
                [Path(app_data) / "npm" / f"{command[0]}{suffix}" for suffix in (".cmd", ".exe")]
            )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _windows_system_executable(name: str) -> str:
    """Resolve one fixed Windows system executable without PATH/COMSPEC."""

    if name not in {"cmd.exe", "taskkill.exe"}:
        raise ModelUnavailable("unsupported Windows system executable")

    import ctypes

    buffer = ctypes.create_unicode_buffer(32_768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise ModelUnavailable("Windows system command processor is unavailable")
    executable = Path(buffer.value) / name
    if not executable.is_file():
        raise ModelUnavailable(f"Windows system {name} is unavailable")
    return str(executable)


def _windows_command_processor() -> str:
    """Resolve the OS system cmd.exe without trusting COMSPEC or PATH."""

    return _windows_system_executable("cmd.exe")


def _platform_cli_argv(command: list[str]) -> list[str]:
    binary = command[0]
    if sys.platform == "win32" and Path(binary).suffix.casefold() in {".bat", ".cmd"}:
        return [
            _windows_command_processor(),
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline(command),
        ]
    return command


def _cli_argv(provider: str, binary: str) -> list[str]:
    return _platform_cli_argv([binary, *CLI_COMMANDS[provider][1:]])


def _preflight_codex_cli(binary: str, *, cwd: str, env: dict[str, str]) -> None:
    """Require every locked-down feature flag before any prompt is transmitted."""

    command = [binary, "features", "list"]
    for feature in CODEX_DISABLED_FEATURES:
        command.extend(("--disable", feature))
    argv = _platform_cli_argv(command)
    try:
        proc = _launch_cli_process(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            cwd=cwd,
            env=env,
            shell=False,
        )
    except OSError as exc:
        raise ModelUnavailable("Codex CLI capability check could not start") from exc
    try:
        proc.communicate(timeout=CODEX_PREFLIGHT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _terminate_cli_process_tree(proc)
        raise ModelUnavailable("Codex CLI capability check timed out") from exc
    except UnicodeError as exc:
        _terminate_cli_process_tree(proc)
        raise ModelUnavailable("Codex CLI capability check returned invalid text") from exc
    if proc.returncode != 0:
        # Unknown --disable names are rejected by `codex features list`. Do not
        # expose its diagnostics, which can include local configuration data.
        raise ModelUnavailable("Codex CLI does not support every required locked-down feature")


def model_available(provider: str, backend: str) -> bool:
    """Return configuration presence only; never make a billed/network call."""

    try:
        provider, backend = validate_route(provider, backend)
        if backend == "cli":
            return _resolve_binary(provider) is not None
        return _has_api_key(provider)
    except (ModelUnavailable, OSError, RuntimeError, ValueError):
        return False


def run_model(
    provider: str,
    backend: str,
    prompt: str,
    *,
    confirmed_external: bool = False,
    model_id: str | None | _ConfiguredModel = _CONFIGURED_MODEL,
    timeout: int = 120,
) -> str:
    """Run one classification request after an explicit per-run consent."""

    if not confirmed_external:
        raise ExternalConsentRequired(
            "external model use requires an explicit preview confirmation"
        )
    provider, backend = validate_route(provider, backend)
    if isinstance(model_id, _ConfiguredModel):
        resolved_model_id = app_config.model_id_for(provider) or None
    else:
        # ``None`` may be an intentional preview-start snapshot. Never re-read
        # config in that case or a batch could silently switch models mid-run.
        resolved_model_id = model_id.strip() if isinstance(model_id, str) else None
        resolved_model_id = resolved_model_id or None
    if backend == "cli":
        return _run_cli(provider, prompt, timeout=timeout)
    return _run_api(provider, prompt, model_id=resolved_model_id, timeout=timeout)


def _run_cli(provider: str, prompt: str, *, timeout: int) -> str:
    command = CLI_COMMANDS.get(provider)
    if not command:
        raise ModelUnavailable(
            f"{provider} CLI cannot accept private input via stdin; choose the api backend"
        )
    binary = _resolve_binary(provider)
    if not binary:
        raise ModelUnavailable(f"{command[0]} CLI is not installed or not on PATH")

    argv = _cli_argv(provider, binary)

    # Force CLI mode to reuse its own signed-in session, not an API key that
    # happened to be inherited by the desktop process.
    stripped = CLI_STRIP_ENV.get(provider, ())
    env = {key: value for key, value in os.environ.items() if key not in stripped}
    with tempfile.TemporaryDirectory(prefix="plaud-folder-route-") as workdir:
        if provider == "codex":
            _preflight_codex_cli(binary, cwd=workdir, env=env)
        proc = _launch_cli_process(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            cwd=workdir,
            env=env,
            shell=False,
        )
        try:
            stdout, _stderr = proc.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _terminate_cli_process_tree(proc)
            raise ModelFailed(f"{provider} CLI timed out") from exc
        except UnicodeError as exc:
            _terminate_cli_process_tree(proc)
            raise ModelFailed(f"{provider} CLI returned invalid UTF-8 text") from exc
    if proc.returncode != 0:
        # Vendor diagnostics can contain recording/account snippets, so do not
        # reflect stderr into the app log or JSON response.
        raise ModelFailed(f"{provider} CLI failed with exit code {proc.returncode}")

    output = stdout.strip()
    if provider == "gemini":
        try:
            document = json.loads(output)
        except (TypeError, json.JSONDecodeError):
            pass
        else:
            if isinstance(document, dict) and isinstance(document.get("response"), str):
                output = document["response"].strip()
    if not output:
        raise ModelFailed(f"{provider} CLI returned no text")
    return output


def _terminate_cli_process_tree(proc: _CliProcess) -> None:
    """Terminate and reap a timed-out CLI, including Windows descendants."""

    terminated = False
    terminate_job = getattr(proc, "terminate_tree", None)
    if callable(terminate_job):
        try:
            terminated = bool(terminate_job())
        except OSError:
            terminated = False
    if sys.platform == "win32" and not terminated:
        try:
            taskkill = _windows_system_executable("taskkill.exe")
            result = subprocess.run(
                [taskkill, "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
                shell=False,
            )
            terminated = result.returncode == 0
        except (ModelUnavailable, OSError, subprocess.SubprocessError):
            terminated = False
    if not terminated:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.communicate(timeout=5)
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.communicate(timeout=5)
        except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
            pass


def _run_api(provider: str, prompt: str, *, model_id: str | None, timeout: int) -> str:
    key = _api_key(provider)
    if not key:
        raise ModelUnavailable(f"no protected API key configured for {provider}")
    if not model_id:
        raise ModelUnavailable(f"no API model id configured for {provider}")

    try:
        if provider == "claude":
            response = _safe_post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model_id,
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=timeout,
            )
            payload = _checked_json(response, provider)
            text = "".join(
                str(part.get("text") or "")
                for part in payload.get("content") or []
                if isinstance(part, dict) and part.get("type") == "text"
            )
        elif provider in {"codex", "grok"}:
            base_url = "https://api.openai.com" if provider == "codex" else "https://api.x.ai"
            response = _safe_post(
                f"{base_url}/v1/responses",
                headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
                json={
                    "model": model_id,
                    "input": prompt,
                    "max_output_tokens": 500,
                    "store": False,
                },
                timeout=timeout,
            )
            text = _responses_text(_checked_json(response, provider))
        elif provider == "gemini":
            response = _safe_post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent",
                headers={"x-goog-api-key": key, "content-type": "application/json"},
                json={
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": 500},
                },
                timeout=timeout,
            )
            payload = _checked_json(response, provider)
            candidates = payload.get("candidates") or []
            parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
            text = "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))
        else:  # pragma: no cover - validate_route already guards this
            raise ModelUnavailable(f"unsupported API provider: {provider}")
    except httpx.HTTPError as exc:
        raise ModelFailed(f"{provider} API request failed") from exc

    text = text.strip()
    if not text:
        raise ModelFailed(f"{provider} API returned no text")
    return text


def _safe_post(
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, Any],
    timeout: int,
) -> httpx.Response:
    """POST without ambient proxies or cross-origin redirects."""

    with httpx.Client(
        trust_env=False,
        follow_redirects=False,
        timeout=timeout,
    ) as client:
        return client.post(url, headers=headers, json=json)


def _checked_json(response: httpx.Response, provider: str) -> dict[str, Any]:
    if response.status_code < 200 or response.status_code >= 300:
        raise ModelFailed(f"{provider} API returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ModelFailed(f"{provider} API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ModelFailed(f"{provider} API returned an unexpected response")
    return payload


def _responses_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                chunks.append(str(part.get("text") or ""))
    if not chunks and isinstance(payload.get("output_text"), str):
        chunks.append(payload["output_text"])
    return "".join(chunks)
