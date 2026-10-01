"""Lifecycle quota for detached /runwindow consoles.

``timeout`` only bounds compile/launch; these pin what bounds the console
afterwards: a live-console cap, a lifetime teardown, a Job Object process
cap, and retention of generated run directories.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

import code_runner


class _Proc:
    _next_pid = 5000

    def __init__(self):
        _Proc._next_pid += 1
        self.pid = _Proc._next_pid
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -1

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def windows(monkeypatch, tmp_path):
    """Pretend to be a Windows console host with a fresh window registry."""
    launched = []
    jobs = []

    def fake_popen(cmd, cwd=None, creationflags=0, env=None, close_fds=False):
        proc = _Proc()
        launched.append(proc)
        return proc

    def fake_job(proc, **kwargs):
        jobs.append(kwargs)
        return object()

    monkeypatch.setattr(code_runner.os, "name", "nt", raising=False)
    monkeypatch.setenv(code_runner.RUN_WINDOW_DIR_ENV, str(tmp_path / "runs"))
    monkeypatch.setattr(code_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(code_runner, "_attach_windows_job", fake_job, raising=False)
    monkeypatch.setattr(code_runner, "_resume_windows_process", lambda proc: None, raising=False)
    monkeypatch.setattr(code_runner, "_close_windows_job", lambda job: None)
    registry = code_runner._WindowRegistry()
    monkeypatch.setattr(code_runner, "_WINDOWS", registry)
    yield launched, jobs
    # Cancel lifetime timers (and wait out an in-flight expiry, which holds
    # the lock) while the fakes are still patched in. A timer outliving the
    # test used to fire against the real ctypes calls with a fake handle.
    registry.shutdown()


def _launch():
    return code_runner.run_code_window("print('hi')", language="python")


def test_live_console_count_is_capped(windows, monkeypatch):
    launched, _jobs = windows
    monkeypatch.setattr(code_runner, "RUN_WINDOW_MAX_LIVE", 2, raising=False)

    assert _launch()["ok"] and _launch()["ok"]
    refused = _launch()

    assert refused["ok"] is False
    assert "already open" in refused["error"]
    assert len(launched) == 2


def test_closed_consoles_free_their_slot(windows, monkeypatch):
    launched, _jobs = windows
    monkeypatch.setattr(code_runner, "RUN_WINDOW_MAX_LIVE", 1, raising=False)
    assert _launch()["ok"]
    launched[0].returncode = 0  # the user closed the console
    assert _launch()["ok"]
    assert len(launched) == 2


def test_console_tree_is_torn_down_after_its_lifetime(windows, monkeypatch):
    launched, _jobs = windows
    torn_down = []
    monkeypatch.setattr(code_runner, "RUN_WINDOW_LIFETIME_SECONDS", 0.2, raising=False)
    monkeypatch.setattr(code_runner, "_terminate_process_tree", torn_down.append)

    out = _launch()
    deadline = time.monotonic() + 10
    while not torn_down and time.monotonic() < deadline:
        time.sleep(0.05)

    assert out["ok"]
    assert torn_down == [launched[0]]
    assert out["window_lifetime"] == 0.2
    assert "closed after" in code_runner.format_window_result(out)


def test_console_is_placed_in_a_process_capped_job(windows):
    _launched, jobs = windows
    assert _launch()["ok"]
    assert jobs == [{"active_process_limit": code_runner.RUN_WINDOW_MAX_PROCESSES}]


def test_console_is_suspended_until_after_job_assignment(windows, monkeypatch):
    events = []
    original_popen = code_runner.subprocess.Popen

    def start(*args, **kwargs):
        events.append(("start", kwargs["creationflags"]))
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(code_runner.subprocess, "Popen", start)
    monkeypatch.setattr(code_runner, "_attach_windows_job",
                        lambda proc, **kwargs: events.append(("attach", proc.pid)) or object())
    monkeypatch.setattr(code_runner, "_resume_windows_process",
                        lambda proc: events.append(("resume", proc.pid)), raising=False)

    assert _launch()["ok"]
    assert events[0][1] & 0x00000004  # CREATE_SUSPENDED
    assert [event[0] for event in events] == ["start", "attach", "resume"]


def test_console_launch_fails_closed_without_job(windows, monkeypatch):
    launched, _jobs = windows
    monkeypatch.setattr(code_runner, "_attach_windows_job", lambda proc, **kwargs: None)

    result = _launch()

    assert result["ok"] is False
    assert "job" in result["error"].lower()
    assert launched[0].returncode == -1
    assert code_runner._WINDOWS.live_count() == 0


def test_generated_run_dirs_are_retained_up_to_a_bound(windows, monkeypatch, tmp_path):
    monkeypatch.setattr(code_runner, "RUN_WINDOW_MAX_RETAINED_DIRS", 3, raising=False)
    base = tmp_path / "runs"
    base.mkdir()
    for index in range(6):
        old = base / ("sonder-window-old%d" % index)
        old.mkdir()
        (old / "snippet.py").write_text("x")
        stamp = 1_000_000 + index
        os.utime(old, (stamp, stamp))
    foreign = base / "operator-notes"
    foreign.mkdir()

    out = _launch()

    assert out["ok"]
    remaining = sorted(p.name for p in base.iterdir() if p.name.startswith("sonder-window-"))
    assert len(remaining) == 3
    assert os.path.basename(out["run_dir"]) in remaining
    assert "sonder-window-old5" in remaining and "sonder-window-old4" in remaining
    assert foreign.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_suspended_windows_process_runs_only_after_job_attachment(tmp_path):
    python = getattr(sys, "_base_executable", "") or sys.executable
    marker = tmp_path / "started.txt"
    proc = subprocess.Popen(
        [python, "-c", "from pathlib import Path; Path(%r).write_text('started')" % str(marker)],
        creationflags=0x00000004, cwd=str(tmp_path), close_fds=True,
    )
    job = None
    try:
        assert not marker.exists()
        job = code_runner._attach_windows_job(proc, active_process_limit=1)
        assert job, "job object was not created/assigned"
        assert not marker.exists()
        code_runner._resume_windows_process(proc)
        assert proc.wait(timeout=30) == 0
        assert marker.read_text() == "started"
    finally:
        code_runner._close_windows_job(job)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_job_active_process_limit_is_enforced_by_windows(tmp_path):
    """The ctypes structure must really set the limit, not silently no-op."""
    # A venv's python.exe is a redirector that spawns the base interpreter as
    # a second process; use the base interpreter so the job holds exactly one.
    python = getattr(sys, "_base_executable", "") or sys.executable
    script = (
        "import subprocess, sys\n"
        "sys.stdin.readline()\n"
        "try:\n"
        "    subprocess.run([sys.executable, '-c', 'pass'], check=True)\n"
        "    print('spawned')\n"
        "except OSError:\n"
        "    print('refused')\n"
    )
    proc = subprocess.Popen(
        [python, "-c", script], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(tmp_path),
    )
    job = code_runner._attach_windows_job(proc, active_process_limit=1)
    try:
        assert job, "job object was not created/assigned"
        out, _ = proc.communicate("go\n", timeout=30)
    finally:
        code_runner._close_windows_job(job)
    assert out.strip() == "refused"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_close_windows_job_never_raises_for_an_unmarshalable_handle():
    """ctypes.ArgumentError is not an OSError; teardown must still swallow it."""
    code_runner._close_windows_job(object())


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_lifetime_expiry_survives_a_bad_job_handle():
    """The expiry runs on a timer thread; an exception there is unhandled."""
    registry = code_runner._WindowRegistry()
    proc = _Proc()
    proc._sonder_job_handle = object()

    class _Timer:
        def cancel(self):
            pass

    registry._windows[1] = {"proc": proc, "job": object(), "timer": _Timer(), "run_dir": None}
    registry._expire(1)
    assert registry._windows == {}
