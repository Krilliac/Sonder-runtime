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


def test_failed_popen_still_closes_windows_job(monkeypatch, tmp_path):
    closed = []
    monkeypatch.setattr(blender_runner.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        blender_runner, "_windows_job",
        lambda _limit: (lambda _proc: None, lambda: closed.append("closed")),
    )

    def broken_popen(*_args, **_kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(blender_runner.subprocess, "Popen", broken_popen)

    with pytest.raises(OSError, match="spawn failed"):
        blender_runner.run_limited(["missing"], cwd=str(tmp_path), env={}, timeout=5)
    assert closed == ["closed"]


def test_windows_timeout_closes_job_before_draining_pipe(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(blender_runner.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        blender_runner, "_windows_job",
        lambda _limit: (lambda _proc: events.append("assign"), lambda: events.append("close_job")),
    )

    class FakeProc:
        returncode = None

        def __init__(self):
            self.calls = 0

        def poll(self):
            return self.returncode

        def kill(self):
            events.append("kill")
            self.returncode = -9

        def communicate(self, timeout=None):
            self.calls += 1
            events.append(("communicate", timeout))
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["x"], timeout)
            return b"partial", b""

    monkeypatch.setattr(blender_runner.subprocess, "Popen", lambda *_a, **_kw: FakeProc())

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        blender_runner.run_limited(["x"], cwd=str(tmp_path), env={}, timeout=1)

    assert exc_info.value.output == b"partial"
    assert events.index("close_job") < events.index(("communicate", 5))
    assert events.count("close_job") == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object pipe-holding descendant test")
def test_windows_timeout_with_pipe_holding_descendant_does_not_deadlock(tmp_path):
    pid_file = tmp_path / "pipe_gc.pid"
    probe = textwrap.dedent(
        """
        import subprocess, sys, time
        gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        open(sys.argv[1], "w").write(str(gc.pid))
        time.sleep(120)
        """
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        blender_runner.run_limited(
            [sys.executable, "-c", probe, str(pid_file)],
            cwd=str(tmp_path), env=dict(os.environ), timeout=2,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 10.0, f"run_limited hung waiting on descendant pipe ({elapsed:.1f}s)"
    grandchild = _grandchild(pid_file)
    try:
        assert _gone(grandchild), "pipe-holding grandchild outlived timeout"
    finally:
        _force_kill(grandchild)
