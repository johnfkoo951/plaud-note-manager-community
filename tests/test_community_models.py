"""Provider routing remains consent-gated and keeps credentials out of argv."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import core.community_models as models


def test_windows_job_runner_forwards_private_input_only_over_stdin() -> None:
    runner = models.Path(models.__file__).with_name("_windows_job_runner.py")
    private_input = b"\xff\x00private recording\n"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(runner),
            "--",
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ],
        input=models._WINDOWS_JOB_START_GATE.encode("ascii") + private_input,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == private_input
    assert result.stderr == b""


def test_windows_job_runner_refuses_eof_without_assignment_gate() -> None:
    runner = models.Path(models.__file__).with_name("_windows_job_runner.py")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(runner),
            "--",
            sys.executable,
            "-c",
            "print('provider must not start')",
        ],
        input=b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 126
    assert result.stdout == b""
    assert result.stderr == b""


def test_windows_job_assigns_process_and_kills_members_on_release() -> None:
    events: list[tuple] = []

    class FakeKernel:
        def OpenProcess(self, access, inherit, pid):
            events.append(("open", access, inherit, pid))
            return 202

        def AssignProcessToJobObject(self, job, process):
            events.append(("assign", job, process))
            return True

        def TerminateJobObject(self, job, exit_code):
            events.append(("terminate", job, exit_code))
            return True

        def CloseHandle(self, handle):
            events.append(("close", handle))
            return True

    job = models._WindowsJob(101, FakeKernel())
    job.assign(303)
    assert job.terminate() is True
    assert job.close() is True

    assert events == [
        (
            "open",
            models._PROCESS_TERMINATE | models._PROCESS_SET_QUOTA,
            False,
            303,
        ),
        ("assign", 101, 202),
        ("close", 202),
        ("terminate", 101, 1),
        ("close", 101),
    ]


def test_run_model_fails_closed_before_credential_or_process_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        models,
        "_api_key",
        lambda _provider: (_ for _ in ()).throw(AssertionError("credential read")),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("process launch")),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("process launch")),
    )

    with pytest.raises(models.ExternalConsentRequired):
        models.run_model("codex", "api", "private recording", confirmed_external=False)


def test_cli_oauth_route_uses_stdin_and_strips_inherited_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-cli")
    monkeypatch.setattr(models.shutil, "which", lambda _name: "/usr/local/bin/claude")

    class FakeProcess:
        returncode = 0
        pid = 101

        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)

        def communicate(self, *, input=None, timeout=None):
            captured["input"] = input
            captured["timeout"] = timeout
            return '{"folder_id":"f"}', ""

    monkeypatch.setattr(models.subprocess, "Popen", FakeProcess)

    output = models.run_model(
        "claude",
        "cli",
        "recording content",
        confirmed_external=True,
    )

    assert output == '{"folder_id":"f"}'
    assert captured["input"] == "recording content"
    assert "recording content" not in captured["argv"]
    assert "must-not-reach-cli" not in captured["argv"]
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert captured["shell"] is False
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "strict"
    assert "--restricted" in captured["argv"]
    assert "--no-session-persistence" in captured["argv"]
    assert "--strict-mcp-config" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--tools") + 1] == ""


def test_codex_cli_is_ephemeral_and_ignores_local_agent_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    monkeypatch.setattr(models.shutil, "which", lambda _name: "/usr/local/bin/codex")

    class FakeProcess:
        returncode = 0
        pid = 102

        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)

        def communicate(self, *, input=None, timeout=None):
            captured["input"] = input
            return '{"folder_id":"f"}', ""

    monkeypatch.setattr(models.subprocess, "Popen", FakeProcess)

    models.run_model("codex", "cli", "private recording", confirmed_external=True)

    assert captured["argv"] == [
        "/usr/local/bin/codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--disable",
        "shell_tool",
        "--disable",
        "unified_exec",
        "--disable",
        "browser_use",
        "--disable",
        "in_app_browser",
        "--disable",
        "apps",
        "--disable",
        "plugins",
        "--disable",
        "hooks",
        "--disable",
        "skill_search",
        "--disable",
        "computer_use",
        "-",
    ]
    assert "private recording" not in captured["argv"]


def test_codex_feature_preflight_rejects_unknown_disable_before_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = "/usr/local/bin/codex"
    captured: dict = {"processes": 0}
    monkeypatch.setattr(models.shutil, "which", lambda _name: binary)

    class RejectedFeatureProcess:
        returncode = 1
        pid = 105

        def __init__(self, argv, **kwargs):
            captured["processes"] += 1
            if captured["processes"] > 1:
                pytest.fail("model process launched after failed feature preflight")
            captured["argv"] = argv
            captured["kwargs"] = kwargs

        def communicate(self, *, input=None, timeout=None):
            captured["input"] = input
            captured["timeout"] = timeout
            return "", "Unknown feature flag: shell_tool"

    monkeypatch.setattr(models.subprocess, "Popen", RejectedFeatureProcess)

    with pytest.raises(models.ModelUnavailable, match="locked-down feature"):
        models.run_model(
            "codex",
            "cli",
            "private recording must never reach Codex",
            confirmed_external=True,
        )

    expected = [binary, "features", "list"]
    for feature in models.CODEX_DISABLED_FEATURES:
        expected.extend(("--disable", feature))
    assert captured["argv"] == expected
    assert captured["input"] is None
    assert captured["timeout"] == models.CODEX_PREFLIGHT_TIMEOUT_SECONDS
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["kwargs"]["errors"] == "strict"
    assert captured["kwargs"]["shell"] is False
    assert "private recording" not in " ".join(captured["argv"])


def test_windows_npm_cmd_uses_trusted_system_cmd_and_keeps_prompt_on_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {"events": []}
    vendor_binary = r"C:\Users\Yohan\AppData\Roaming\npm\claude.CMD"
    system_cmd = r"C:\Windows\System32\cmd.exe"
    monkeypatch.setattr(models.sys, "platform", "win32")
    monkeypatch.setenv("COMSPEC", r"C:\untrusted\cmd.exe")
    monkeypatch.setattr(models.shutil, "which", lambda _name: vendor_binary)
    monkeypatch.setattr(models, "_windows_command_processor", lambda: system_cmd)

    class FakeJob:
        def assign(self, pid):
            captured["events"].append(("assign", pid))

        def close(self):
            captured["events"].append(("close",))
            return True

        def terminate(self):
            captured["events"].append(("terminate-job",))
            return True

    monkeypatch.setattr(models, "_create_windows_kill_on_close_job", FakeJob)

    class FakeProcess:
        returncode = 0
        pid = 103

        def __init__(self, argv, **kwargs):
            captured["events"].append(("popen",))
            captured["argv"] = argv
            captured.update(kwargs)

        def communicate(self, *, input=None, timeout=None):
            captured["events"].append(("communicate", input, timeout))
            captured["input"] = input
            return '{"folder_id":"f"}', ""

    monkeypatch.setattr(models.subprocess, "Popen", FakeProcess)

    models.run_model("claude", "cli", "private recording", confirmed_external=True)

    vendor_argv = [vendor_binary, *models.CLI_COMMANDS["claude"][1:]]
    assert captured["argv"][:7] == [
        models.sys.executable,
        "-I",
        "-S",
        "-B",
        str(models.Path(models.__file__).with_name("_windows_job_runner.py")),
        "--",
        system_cmd,
    ]
    assert captured["argv"][7:] == [
        "/d",
        "/s",
        "/c",
        subprocess.list2cmdline(vendor_argv),
    ]
    assert captured["events"] == [
        ("popen",),
        ("assign", 103),
        (
            "communicate",
            models._WINDOWS_JOB_START_GATE + "private recording",
            120,
        ),
        ("terminate-job",),
    ]
    assert captured["input"] == models._WINDOWS_JOB_START_GATE + "private recording"
    assert "private recording" not in " ".join(captured["argv"])
    assert system_cmd != captured["env"]["COMSPEC"]
    assert captured["stdin"] is subprocess.PIPE
    assert captured["close_fds"] is True
    assert captured["shell"] is False
    assert captured["encoding"] == "utf-8"


def test_windows_timeout_terminates_job_tree_and_reaps_gated_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {"events": [], "communicate": [], "kill": 0}
    vendor_binary = r"C:\Users\Yohan\AppData\Roaming\npm\codex.cmd"
    system_cmd = r"C:\Windows\System32\cmd.exe"
    monkeypatch.setattr(models.sys, "platform", "win32")
    monkeypatch.setattr(models.shutil, "which", lambda _name: vendor_binary)
    monkeypatch.setattr(models, "_windows_command_processor", lambda: system_cmd)
    monkeypatch.setattr(models, "_preflight_codex_cli", lambda *_args, **_kwargs: None)

    class FakeJob:
        closed = False

        def assign(self, pid):
            captured["events"].append(("assign", pid))

        def close(self):
            captured["events"].append(("close",))
            self.closed = True
            return True

        def terminate(self):
            if self.closed:
                return True
            captured["events"].append(("terminate-job",))
            self.closed = True
            return True

    monkeypatch.setattr(models, "_create_windows_kill_on_close_job", FakeJob)

    class TimedOutProcess:
        returncode = 1
        pid = 4242

        def __init__(self, argv, **kwargs):
            captured["events"].append(("popen",))
            captured["popen_argv"] = argv
            captured["popen_kwargs"] = kwargs

        def communicate(self, *, input=None, timeout=None):
            captured["communicate"].append((input, timeout))
            if len(captured["communicate"]) == 1:
                raise subprocess.TimeoutExpired(self.pid, timeout)
            return "", ""

        def kill(self):
            captured["kill"] += 1

    monkeypatch.setattr(models.subprocess, "Popen", TimedOutProcess)
    monkeypatch.setattr(
        models.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Job cleanup must precede taskkill fallback"),
    )

    with pytest.raises(models.ModelFailed, match="timed out"):
        models.run_model(
            "codex",
            "cli",
            "한국어 비공개 녹음",
            confirmed_external=True,
            timeout=7,
        )

    assert captured["popen_kwargs"]["encoding"] == "utf-8"
    assert captured["communicate"] == [
        (models._WINDOWS_JOB_START_GATE + "한국어 비공개 녹음", 7),
        (None, 5),
    ]
    assert captured["events"] == [
        ("popen",),
        ("assign", 4242),
        ("terminate-job",),
    ]
    assert captured["kill"] == 0


def test_windows_job_assignment_failure_never_releases_private_input_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {"events": []}
    monkeypatch.setattr(models.sys, "platform", "win32")
    monkeypatch.setattr(models.shutil, "which", lambda _name: r"C:\tools\claude.exe")

    class FakeJob:
        def assign(self, pid):
            captured["events"].append(("assign-failed", pid))
            raise OSError("job assignment rejected")

        def close(self):
            captured["events"].append(("close",))
            return True

    class FakeStream:
        def close(self):
            captured["events"].append(("stream-close",))

    class GatedProcess:
        returncode = None
        pid = 5150
        stdin = FakeStream()
        stdout = FakeStream()
        stderr = FakeStream()

        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            captured["events"].append(("popen",))

        def communicate(self, *, input=None, timeout=None):
            pytest.fail("failed Job assignment must never transmit private input")

        def kill(self):
            captured["events"].append(("kill-gated-launcher",))

        def wait(self, *, timeout=None):
            captured["events"].append(("wait", timeout))
            return 1

    monkeypatch.setattr(models, "_create_windows_kill_on_close_job", FakeJob)
    monkeypatch.setattr(models.subprocess, "Popen", GatedProcess)

    with pytest.raises(OSError, match="job assignment rejected"):
        models.run_model(
            "claude",
            "cli",
            "private text that must not cross the failed gate",
            confirmed_external=True,
        )

    assert "private text" not in " ".join(captured["argv"])
    assert captured["events"] == [
        ("popen",),
        ("assign-failed", 5150),
        ("kill-gated-launcher",),
        ("wait", 5),
        ("stream-close",),
        ("stream-close",),
        ("stream-close",),
        ("close",),
    ]


def test_taskkill_is_only_the_fallback_when_no_job_tree_handle_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {"communicate": 0, "kill": 0}
    taskkill = r"C:\Windows\System32\taskkill.exe"
    monkeypatch.setattr(models.sys, "platform", "win32")
    monkeypatch.setattr(models, "_windows_system_executable", lambda _name: taskkill)

    class LegacyProcess:
        pid = 6161
        returncode = None

        def communicate(self, *, input=None, timeout=None):
            captured["communicate"] += 1
            return "", ""

        def kill(self):
            captured["kill"] += 1

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(models.subprocess, "run", fake_run)

    models._terminate_cli_process_tree(LegacyProcess())

    assert captured["argv"] == [taskkill, "/PID", "6161", "/T", "/F"]
    assert captured["kwargs"]["shell"] is False
    assert captured["communicate"] == 1
    assert captured["kill"] == 0


def test_cli_invalid_utf8_fails_closed_without_echoing_private_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {"terminated": 0}
    monkeypatch.setattr(models.shutil, "which", lambda _name: "/usr/local/bin/claude")

    class InvalidUtf8Process:
        returncode = 0
        pid = 104

        def __init__(self, _argv, **kwargs):
            captured["encoding"] = kwargs["encoding"]
            captured["errors"] = kwargs["errors"]

        def communicate(self, *, input=None, timeout=None):
            raise UnicodeDecodeError("utf-8", b"\xffprivate", 0, 1, "invalid start byte")

    monkeypatch.setattr(models.subprocess, "Popen", InvalidUtf8Process)
    monkeypatch.setattr(
        models,
        "_terminate_cli_process_tree",
        lambda _proc: captured.__setitem__("terminated", captured["terminated"] + 1),
    )

    with pytest.raises(models.ModelFailed) as exc_info:
        models.run_model(
            "claude",
            "cli",
            "한국어 비공개 녹음",
            confirmed_external=True,
        )

    assert captured == {"encoding": "utf-8", "errors": "strict", "terminated": 1}
    assert "private" not in str(exc_info.value)


@pytest.mark.parametrize("provider", ["gemini", "grok"])
def test_api_only_provider_is_rejected_centrally(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models.shutil, "which", lambda _name: f"/usr/local/bin/{provider}")
    with pytest.raises(models.ModelUnavailable, match="api backend only"):
        models.validate_route(provider, "cli")
    assert models.model_available(provider, "cli") is False

    with pytest.raises(models.ModelUnavailable, match="api backend only"):
        models.run_model(
            provider,
            "cli",
            "untrusted recording must not gain tool access",
            confirmed_external=True,
        )


def test_cli_discovery_uses_known_path_when_gui_path_is_minimal(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o700)
    monkeypatch.setattr(models.shutil, "which", lambda _name: None)
    monkeypatch.setitem(models.CLI_FALLBACK_PATHS, "codex", (binary,))

    assert os.access(binary, os.X_OK)
    assert models.model_available("codex", "cli") is True


def test_api_availability_uses_boolean_status_without_retrieving_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(models, "_has_api_key", lambda provider: provider == "codex")
    monkeypatch.setattr(
        models,
        "_api_key",
        lambda _provider: (_ for _ in ()).throw(AssertionError("must not retrieve key")),
    )

    assert models.model_available("codex", "api") is True


def test_api_route_reads_protected_key_but_never_passes_it_as_parameter_or_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    monkeypatch.setattr(models, "_api_key", lambda provider: "protected-secret")

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"output": [{"content": [{"type": "output_text", "text": '{"folder_id":"x"}'}]}]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(models, "_safe_post", fake_post)

    output = models.run_model(
        "codex",
        "api",
        "recording content",
        confirmed_external=True,
        model_id="gpt-test",
    )

    assert output == '{"folder_id":"x"}'
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert "protected-secret" not in captured["url"]
    assert captured["headers"]["authorization"] == "Bearer protected-secret"
    assert captured["json"]["store"] is False
    assert captured["json"]["input"] == "recording content"


def test_gemini_api_key_is_a_header_not_query_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    monkeypatch.setattr(models, "_api_key", lambda provider: "gemini-secret")

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {
                "candidates": [{"content": {"parts": [{"text": json.dumps({"folder_id": "g"})}]}}]
            }

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(models, "_safe_post", fake_post)

    models.run_model(
        "gemini",
        "api",
        "recording",
        confirmed_external=True,
        model_id="gemini-test",
    )

    assert "gemini-secret" not in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == "gemini-secret"


def test_provider_error_does_not_echo_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "_api_key", lambda provider: "protected-secret")

    class Response:
        status_code = 401
        text = "protected-secret and recording excerpt"

        @staticmethod
        def json() -> dict:
            return {}

    monkeypatch.setattr(models, "_safe_post", lambda *_args, **_kwargs: Response())

    with pytest.raises(models.ModelFailed) as exc_info:
        models.run_model(
            "codex",
            "api",
            "recording excerpt",
            confirmed_external=True,
            model_id="gpt-test",
        )
    assert "protected-secret" not in str(exc_info.value)
    assert "recording excerpt" not in str(exc_info.value)


def test_http_client_disables_ambient_proxies_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class Client:
        def __init__(self, **kwargs) -> None:
            captured["init"] = kwargs

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_args) -> None:
            return None

        def post(self, url, **kwargs):
            captured["url"] = url
            captured["post"] = kwargs
            return object()

    monkeypatch.setattr(models.httpx, "Client", Client)

    result = models._safe_post(
        "https://api.openai.com/v1/responses",
        headers={"authorization": "Bearer protected"},
        json={"input": "local fixture"},
        timeout=12,
    )

    assert result is not None
    assert captured["init"] == {
        "trust_env": False,
        "follow_redirects": False,
        "timeout": 12,
    }
    assert captured["url"] == "https://api.openai.com/v1/responses"
