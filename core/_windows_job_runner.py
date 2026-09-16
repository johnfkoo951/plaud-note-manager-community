"""Internal Windows provider launcher; no external or user-facing entry point.

The parent assigns this process to a kill-on-close Job Object before it closes
stdin.  Reading stdin in full is therefore the gate that prevents a provider
from starting before the Job assignment has completed.
"""

from __future__ import annotations

import sys

_START_GATE = b"PLAUD_WINDOWS_JOB_ASSIGNED_V1\n"


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        return 2

    gated_input = sys.stdin.buffer.read()
    # EOF is not authorization to start.  If the parent crashes before Job
    # assignment, its pipe closes without this token and no provider can spawn.
    if not gated_input.startswith(_START_GATE):
        return 126
    private_input = gated_input[len(_START_GATE) :]

    # Keep startup before the pipe gate limited to the standard runtime.  This
    # import is intentionally delayed until after the parent releases the gate.
    import subprocess

    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
        )
        stdout, stderr = process.communicate(input=private_input)
    except OSError:
        return 127
    sys.stdout.buffer.write(stdout)
    sys.stderr.buffer.write(stderr)
    return process.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())
