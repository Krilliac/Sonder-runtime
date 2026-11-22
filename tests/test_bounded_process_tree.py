"""run_bounded must leave no descendant of the probe running, whatever ends it."""
from __future__ import annotations

import sys
import textwrap
import time

import pytest

from sonder_runtime.adapters import process_liveness
from sonder_runtime.adapters.host_tools import bounded_process

# Prints the grandchild pid, then either exits at once or sleeps.
_PROBE = textwrap.dedent(
    """
    import subprocess, sys, time
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
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


def _first_pid(output):
    return int(output.split()[0])


def test_grandchild_of_a_probe_that_exited_normally_is_removed():
    result = bounded_process.run_bounded(
        [sys.executable, "-c", _PROBE, "exit"], timeout_seconds=20,
    )
    grandchild = _first_pid(result.output)
    try:
        assert result.outcome == "ok"
        assert _gone(grandchild), "grandchild outlived run_bounded"
    finally:
        _kill_if_alive(grandchild)


def test_grandchild_of_a_probe_that_timed_out_is_removed():
    result = bounded_process.run_bounded(
        [sys.executable, "-c", _PROBE, "sleep"], timeout_seconds=2,
    )
    grandchild = _first_pid(result.output)
    try:
        assert result.outcome == "timeout"
        assert _gone(grandchild), "grandchild outlived run_bounded"
    finally:
        _kill_if_alive(grandchild)


def _kill_if_alive(pid):
    if not process_liveness.pid_alive(pid):
        return
    import os
    import signal
    try:
        if os.name == "nt":
            import subprocess
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pytest.fail("could not clean up leaked grandchild %d" % pid)


def test_reader_start_failure_terminates_child_and_closes_tree(monkeypatch):
    events = []

    class FakeStream:
        def read(self, *_args):
            return b""

        def close(self):
            events.append("close_stream")

    class FakeProc:
        pid = 1234
        returncode = None

        def __init__(self):
            self.stdout = FakeStream()
            self.stderr = FakeStream()

        def poll(self):
            return self.returncode

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

    monkeypatch.setattr(bounded_process.subprocess, "Popen", lambda *_a, **_kw: FakeProc())
    monkeypatch.setattr(bounded_process.process_tree, "is_real_popen", lambda _p: True)
    monkeypatch.setattr(
        bounded_process.process_tree.ProcessTree, "owning", staticmethod(lambda _p: FakeTree()),
    )
    monkeypatch.setattr(bounded_process.runtime_threads, "Thread", FailingThread)

    with pytest.raises(RuntimeError, match="thread cap reached"):
        bounded_process.run_bounded(["fake"], timeout_seconds=5)

    assert "terminate" in events
    assert "close_tree" in events
    assert "join_unstarted" not in events
