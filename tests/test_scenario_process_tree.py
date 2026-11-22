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


def test_reader_start_failure_kills_reaps_child_and_closes_tree(monkeypatch, tmp_path):
    import pytest
    import sonder_runtime.adapters.scenario_validation.process as sv_process

    events = []

    class FakeStream:
        def read(self, *_args):
            return b""

    class FakeProc:
        pid = 5678
        returncode = None

        def __init__(self):
            self.stdout = FakeStream()
            self.stderr = FakeStream()

        def poll(self):
            return self.returncode

        def kill(self):
            events.append("kill")
            self.returncode = -9

        def wait(self, timeout=None):
            events.append("wait")
            self.returncode = -9
            return self.returncode

    class FakeTree:
        def terminate(self, **_kwargs):
            events.append("terminate")

        def has_survivors(self):
            return False

        def close(self):
            events.append("close_tree")

    class FailingThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread cap reached")

        def join(self, timeout=None):
            events.append("join_unstarted")

    monkeypatch.setattr(sv_process.subprocess, "Popen", lambda *_a, **_kw: FakeProc())
    monkeypatch.setattr(sv_process.process_tree, "is_real_popen", lambda _p: True)
    monkeypatch.setattr(
        sv_process.process_tree.ProcessTree, "owning", staticmethod(lambda _p: FakeTree()),
    )
    monkeypatch.setattr(sv_process, "owned_runtime_thread", FailingThread)

    with pytest.raises(RuntimeError, match="thread cap reached"):
        ProcessAdapter().run(("fake",), cwd=tmp_path, timeout_seconds=5)

    assert "terminate" in events
    assert "wait" in events
    assert "close_tree" in events
    assert "join_unstarted" not in events
