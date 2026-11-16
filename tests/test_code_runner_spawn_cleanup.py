"""RC5: _run_process must not leave a live child (or its job) behind.

stdin was encoded only after the child and its Job Object existed, and only
TimeoutExpired was handled after the spawn. An unpaired surrogate in stdin
raised UnicodeEncodeError there and the child kept running, its job handle
open: the events were only ['spawn', 'attach_job'].
"""
import sys

import pytest

import code_runner


class _Proc:
    def __init__(self, events):
        self.events = events
        self.returncode = None
        self.pid = 4242
        self.stdin = self.stdout = self.stderr = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.events.append("wait")
        self.returncode = -9
        return self.returncode

    def kill(self):
        self.events.append("kill")
        self.returncode = -9

    def communicate(self, input=None, timeout=None):
        raise RuntimeError("post-spawn failure")


@pytest.fixture
def events(monkeypatch):
    events = []
    job = object()

    def fake_popen(*args, **kwargs):
        events.append("spawn")
        return _Proc(events)

    def fake_attach(proc, **kwargs):
        events.append("attach_job")
        return job

    def fake_terminate(proc):
        events.append("terminate")

    def fake_close(handle):
        if handle is job:
            events.append("close_job")

    monkeypatch.setattr(code_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(code_runner, "_attach_windows_job", fake_attach)
    monkeypatch.setattr(code_runner, "_terminate_process_tree", fake_terminate)
    monkeypatch.setattr(code_runner, "_close_windows_job", fake_close)
    return events


def test_unencodable_stdin_is_refused_before_spawn(events):
    out = code_runner.run_code("print(1)", stdin="\ud800")

    assert events == []
    assert out["ok"] is False
    assert out["returncode"] is None
    assert "stdin" in out["error"]


def test_failure_after_spawn_tears_down_and_closes_the_job(events, monkeypatch, tmp_path):
    def broken_collect(*args, **kwargs):
        raise RuntimeError("post-spawn failure")

    monkeypatch.setattr(code_runner, "_collect_output", broken_collect, raising=False)

    with pytest.raises(RuntimeError, match="post-spawn failure"):
        code_runner._run_process(
            [sys.executable, "-c", "pass"], str(tmp_path), "", 10, "python",
        )

    assert events[:2] == ["spawn", "attach_job"]
    assert "terminate" in events and "wait" in events
    assert events.count("close_job") == 1
    assert events.index("close_job") > events.index("terminate")


def test_interrupt_after_spawn_tears_down_too(events, monkeypatch, tmp_path):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(code_runner, "_collect_output", interrupted, raising=False)
    monkeypatch.setattr(
        _Proc, "communicate",
        lambda self, input=None, timeout=None: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        code_runner._run_process(
            [sys.executable, "-c", "pass"], str(tmp_path), "", 10, "python",
        )

    assert "terminate" in events
    assert events.count("close_job") == 1
