import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("settling", [(0, (258,)), (1, (0,))])
def test_zero_accounting_waits_for_exact_retained_handles(monkeypatch, force, settling):
    from sonder_runtime.adapters.extensions.memory_limits import _WindowsJobToken
    token = _WindowsJobToken(123, lambda handle: True)
    observations = iter([settling, (0, (0,))])
    monkeypatch.setattr(token, "_observe", lambda: next(observations), raising=False)
    proof = token.quiesce(force=force)
    assert proof.complete
    assert not proof.forced
    assert token.cleanup_observation == (0, (0,))


def test_invalid_retained_handle_never_proves_cleanup(monkeypatch):
    from sonder_runtime.adapters.extensions.memory_limits import _WindowsJobToken
    token = _WindowsJobToken(123, lambda handle: True)
    monkeypatch.setattr(token, "_observe", lambda: (0, (0xFFFFFFFF,)), raising=False)
    assert not token.quiesce(force=False).complete
    monkeypatch.setattr(token, "_observe", lambda: (0, (0,)))
    assert not token.quiesce(force=False).complete


@pytest.mark.parametrize("force", [False, True])
def test_handle_wait_uses_one_deadline(monkeypatch, force):
    from types import SimpleNamespace
    from sonder_runtime.adapters.extensions import memory_limits
    token = memory_limits._WindowsJobToken(123, lambda handle: True)
    clock = [0.0]
    def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(memory_limits, "time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(token, "_observe", lambda: (0, (258,)))
    assert not token.quiesce(force=force).complete
    assert clock[0] == 3
    assert token.cleanup_observation == (0, (258,))


def test_failed_job_handle_close_retains_the_owned_handle(monkeypatch):
    from sonder_runtime.adapters.extensions.memory_limits import (
        _WindowsJobToken,
        ExtensionMemoryLimitError,
    )

    token = _WindowsJobToken(123, lambda handle: False)
    monkeypatch.setattr(token, "_observe", lambda: (0, (0,)))
    with pytest.raises(ExtensionMemoryLimitError):
        token.close()
    assert token._handle == 123
    token._close_handle = lambda handle: True
    token.close()
    assert token._handle is None


def test_process_handle_close_failure_retains_job_and_retries(monkeypatch):
    from sonder_runtime.adapters.extensions.memory_limits import _WindowsJobToken
    calls = []
    token = _WindowsJobToken(123, lambda h: calls.append(h) or False)
    token._process_handles = [(1, 456)]
    monkeypatch.setattr(token, "_observe", lambda: (0, (0,)))
    with pytest.raises(Exception, match="process handle close"):
        token.close()
    assert calls == [456] and token._handle == 123
    assert token._process_handles == [(1, 456)]
    token._close_handle = lambda h: calls.append(h) or True
    token.close()
    assert calls == [456, 456, 123]
    assert token._handle is None


def test_unregistered_abort_releases_without_cleanup_proof():
    from sonder_runtime.adapters.extensions.memory_limits import _WindowsJobToken
    calls = []
    token = _WindowsJobToken(123, lambda h: calls.append(("close", h)) or True,
                             terminate=lambda h: calls.append(("terminate", h)) or True)
    token._process_handles = [(1, 456)]
    token._abort_unregistered()
    assert calls == [("terminate", 123), ("close", 456), ("close", 123)]
    assert not token._quiescent_proved
    assert token._handle is None
    token._published = True
    with pytest.raises(Exception, match="published job"):
        token._abort_unregistered()


def test_unregistered_abort_attempts_job_after_failed_process_release():
    from sonder_runtime.adapters.extensions.memory_limits import _WindowsJobToken
    calls = []
    def close(handle):
        calls.append(handle)
        return handle != 456
    token = _WindowsJobToken(123, close, terminate=lambda h: True)
    token._process_handles = [(1, 456), (2, 789)]
    with pytest.raises(Exception, match="cleanup incomplete"):
        token._abort_unregistered()
    assert calls == [456, 789, 123]
    assert token._handle is None
    assert token._process_handles == [(1, 456)]
    assert not token._quiescent_proved


from sonder_runtime.adapters.extensions.memory_limits import (
    NativeExtensionMemoryLimiter,
)


@pytest.mark.skipif(os.name != "nt", reason="actual Windows Job attachment required")
def test_failed_initial_observation_aborts_without_publishing(monkeypatch):
    from sonder_runtime.adapters.extensions.memory_limits import (
        _WindowsJobToken, ExtensionMemoryLimitError,
    )
    limiter = NativeExtensionMemoryLimiter()
    argv = (sys.executable, "-c", "import time; time.sleep(30)")
    prepared = limiter.prepare_process_job("failed-attach", argv, None, 4)
    process = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=prepared.launch_options["creationflags"] | subprocess.CREATE_NO_WINDOW,
    )
    tokens = []
    original_abort = _WindowsJobToken._abort_unregistered
    def unavailable(token):
        raise ExtensionMemoryLimitError("initial observation unavailable")
    def abort(token):
        tokens.append(token)
        return original_abort(token)
    monkeypatch.setattr(_WindowsJobToken, "_observe", unavailable)
    monkeypatch.setattr(_WindowsJobToken, "_abort_unregistered", abort)
    try:
        with pytest.raises(ExtensionMemoryLimitError, match="initial observation"):
            limiter.apply_process_limits(process, None, 4)
        process.wait(timeout=3)
        assert len(tokens) == 1
        assert tokens[0]._handle is None
        assert tokens[0]._process_handles == []
        assert not tokens[0]._published
        assert not tokens[0]._quiescent_proved
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


@pytest.mark.skipif(os.name != "nt", reason="actual Windows Job Object required")
def test_native_job_proves_live_then_empty_without_pid_census():
    limiter = NativeExtensionMemoryLimiter()
    argv = (sys.executable, "-c", "import time; time.sleep(30)")
    prepared = limiter.prepare_process_job("owner-fixture", argv, None, 4)
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=prepared.launch_options["creationflags"]
        | subprocess.CREATE_NO_WINDOW,
    )
    token = None
    try:
        token = limiter.apply_process_limits(process, None, 4)
        limiter.resume(process)
        assert not token.quiesce(force=False).complete
        proof = token.quiesce(force=True)
        assert proof.complete and proof.forced
        process.wait(timeout=3)
        assert token.quiesce(force=False).complete
    finally:
        if token is not None:
            token.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


@pytest.mark.skipif(os.name != "nt", reason="actual Windows descendants required")
@pytest.mark.parametrize("attempt", range(5))
def test_job_cleanup_includes_actual_descendant(tmp_path, attempt):
    import time
    from sonder_runtime.adapters.process_liveness import (
        process_identity,
        probe_process,
        PROCESS_DEAD,
    )

    marker = tmp_path / "descendant.txt"
    script = "import subprocess,sys,time; from pathlib import Path; child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
    argv = (sys._base_executable, "-c", script, str(marker))
    limiter = NativeExtensionMemoryLimiter()
    prepared = limiter.prepare_process_job("descendant-fixture", argv, None, 4)
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=prepared.launch_options["creationflags"]
        | subprocess.CREATE_NO_WINDOW,
    )
    token = None
    try:
        token = limiter.apply_process_limits(process, None, 4)
        limiter.resume(process)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        child_pid = int(marker.read_text())
        child_identity = process_identity(child_pid)
        assert child_identity
        proof = token.quiesce(force=True)
        assert proof.complete, (proof, token.cleanup_observation)
        active, states = token.cleanup_observation
        assert active == 0 and len(states) >= 2 and all(state == 0 for state in states)
        process.wait(timeout=3)
        assert probe_process(child_pid, child_identity)[0] == PROCESS_DEAD
    finally:
        if token is not None:
            token.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
