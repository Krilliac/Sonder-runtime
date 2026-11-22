"""run_limited must not leave a descendant of a timed-out or finished build."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from sonder_runtime.adapters import process_liveness
from sonder_runtime.adapters.meshgen import blender_runner

_PROBE = textwrap.dedent(
    """
    import subprocess, sys, time
    grandchild = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    open(sys.argv[1], "w").write(str(grandchild.pid))
    if sys.argv[2] == "sleep":
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


def _grandchild(pid_file):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_file.read_text().strip():
            return int(pid_file.read_text().strip())
        time.sleep(0.05)
    pytest.fail("grandchild pid was never published")


def test_grandchild_of_a_timed_out_build_is_removed(tmp_path):
    pid_file = tmp_path / "gc.pid"
    with pytest.raises(subprocess.TimeoutExpired):
        blender_runner.run_limited(
            [sys.executable, "-c", _PROBE, str(pid_file), "sleep"],
            cwd=str(tmp_path), env=dict(os.environ), timeout=3,
        )
    grandchild = _grandchild(pid_file)
    try:
        assert _gone(grandchild), "grandchild outlived the timed-out build"
    finally:
        _force_kill(grandchild)


def test_grandchild_of_a_build_that_exited_is_removed(tmp_path):
    pid_file = tmp_path / "gc.pid"
    code, _out, _err = blender_runner.run_limited(
        [sys.executable, "-c", _PROBE, str(pid_file), "exit"],
        cwd=str(tmp_path), env=dict(os.environ), timeout=30,
    )
    grandchild = _grandchild(pid_file)
    try:
        assert code == 0
        assert _gone(grandchild), "grandchild outlived the build"
    finally:
        _force_kill(grandchild)
