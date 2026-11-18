"""ProcessAdapter (scenario validation) leaves no grandchild behind."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

from sonder_runtime.adapters import process_liveness
from sonder_runtime.adapters.scenario_validation import ProcessAdapter

_PROBE = textwrap.dedent(
    """
    import subprocess, sys, time
    grandchild = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(grandchild.pid, flush=True)
    if sys.argv[1] == "sleep":
        time.sleep(120)
    """
)


def _gone(pid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_liveness.pid_alive(pid):
            return True
        time.sleep(0.05)
    return not process_liveness.pid_alive(pid)


def _force_kill(pid):
    if not process_liveness.pid_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _run(mode, timeout, tmp_path):
    result = ProcessAdapter().run(
        (sys.executable, "-c", _PROBE, mode), cwd=tmp_path, timeout_seconds=timeout,
    )
    return result, int(result.stdout.split()[0])


def test_grandchild_of_a_command_that_exited_normally_is_removed(tmp_path):
    result, grandchild = _run("exit", 30, tmp_path)
    try:
        assert result.returncode == 0
        assert _gone(grandchild), "grandchild outlived the validation command"
    finally:
        _force_kill(grandchild)


def test_grandchild_of_a_command_that_timed_out_is_removed(tmp_path):
    result, grandchild = _run("sleep", 2, tmp_path)
    try:
        assert result.timed_out is True
        assert _gone(grandchild), "grandchild outlived the validation command"
    finally:
        _force_kill(grandchild)
