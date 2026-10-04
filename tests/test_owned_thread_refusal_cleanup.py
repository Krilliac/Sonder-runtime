"""A refused owned runtime thread must not strand the state it was guarding.

``runtime_threads.Thread`` raises ``ThreadOwnershipRefused`` when the managed
owner is at capacity, stopped, or unresolved.  Callers that set up state
before creating or starting the thread must roll that state back: a child
process that no thread will ever wait on or kill, or an in-flight marker
that no worker will ever clear.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.filesystem.workbench as workbench
from sonder_runtime.platform.runtime_threads import (
    Thread as native_owned_thread,
    ThreadOwnershipRefused,
)


def _refuse_after(monkeypatch, module, allowed):
    calls = {"n": 0}

    def factory(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > allowed:
            raise ThreadOwnershipRefused("runtime thread capacity exhausted")
        return native_owned_thread(*args, **kwargs)

    monkeypatch.setattr(module, "owned_runtime_thread", factory)
    return calls


@pytest.mark.parametrize("allowed", [0, 1, 2])
def test_run_program_reaps_child_when_io_thread_is_refused(
    monkeypatch, tmp_path, allowed,
):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(
        file_ops.runtime_paths, "default_home", lambda: tmp_path / "home",
    )
    script = tmp_path / "linger.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    spawned = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(workbench.subprocess, "Popen", recording_popen)
    _refuse_after(monkeypatch, workbench, allowed)

    with pytest.raises(ThreadOwnershipRefused):
        workbench.run_program(
            sys.executable, args_json=[str(script)], timeout=30,
        )

    # spawned[0] is the program; later entries are the taskkill tree kill.
    proc = spawned[0]
    try:
        # The child must already be dead and reaped, not left to run 60s.
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


@pytest.mark.real_prewarm  # exercises server.prewarm_model itself (see #583)
def test_prewarm_releases_inflight_marker_when_worker_is_refused(monkeypatch):
    import server

    monkeypatch.setattr(
        server.sonder_speculation, "speculation_enabled", lambda: True,
    )
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict: ("prewarm-probe-model", False, False, "general"),
    )
    monkeypatch.setattr(server, "_bridge_provider_for_tier", lambda tier: None)
    monkeypatch.setattr(server, "_auto_model_context", lambda model: 8192)
    posts = []
    monkeypatch.setattr(server, "_post", lambda *a, **k: posts.append(a))
    _refuse_after(monkeypatch, server, 0)

    assert server.prewarm_model("general") is False
    assert posts == []
    # Capacity frees up: the next prewarm must be able to run, not be
    # refused forever because the first attempt stayed marked in flight.
    started = []

    class _Immediate:
        def __init__(self, target, **kwargs):
            self._target = target

        def start(self):
            started.append(True)
            self._target()

    monkeypatch.setattr(server, "owned_runtime_thread", _Immediate)
    assert server.prewarm_model("general") is True
    assert started == [True]
    assert len(posts) == 1
    assert posts[0][0] == "/api/chat"
    assert posts[0][1]["options"]["num_ctx"] == 8192
    assert posts[0][1]["options"]["num_predict"] == 1


def test_selfmod_lease_is_released_when_heartbeat_is_refused(monkeypatch):
    import server

    released = []
    monkeypatch.setattr(server.selfmod, "claim", lambda run_id: "owner-1")
    monkeypatch.setattr(
        server.selfmod, "release",
        lambda run_id, owner: released.append((run_id, owner)),
    )
    _refuse_after(monkeypatch, server, 0)

    with pytest.raises(ThreadOwnershipRefused):
        with server._selfmod_operator_lease("run-7"):
            pytest.fail("the lease body must not run without its heartbeat")
    # Otherwise every later operator call on this run is refused as busy
    # until the ledger lease expires.
    assert released == [("run-7", "owner-1")]
