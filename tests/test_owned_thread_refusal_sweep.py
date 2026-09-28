"""Refused owned runtime threads must not strand caller state.

``runtime_threads.Thread`` (and the ``start()`` of what it returns) raises
``ThreadOwnershipRefused`` when the managed owner is at capacity, stopped or
unresolved.  Each adapter below sets up state first (a running child process,
an in-flight marker, a persister waiting for writers) and must roll it back
when the factory refuses.  Sibling of ``test_owned_thread_refusal_cleanup``
(prewarm, workbench run_program, selfmod lease).
"""
from __future__ import annotations

import io
import subprocess
import sys
import threading

import pytest

from sonder_runtime.platform.runtime_threads import (
    Thread as native_owned_thread,
    ThreadOwnershipRefused,
)

_SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def _refuse_after(monkeypatch, module, allowed):
    calls = {"n": 0}

    def factory(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > allowed:
            raise ThreadOwnershipRefused("runtime thread capacity exhausted")
        return native_owned_thread(*args, **kwargs)

    monkeypatch.setattr(module, "owned_runtime_thread", factory)
    return calls


def _recording_popen(monkeypatch, module_subprocess):
    spawned = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(module_subprocess, "Popen", popen)
    return spawned


def _assert_reaped(proc):
    try:
        assert proc.poll() is not None, "refused worker left the child running"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


@pytest.mark.parametrize("allowed", [0, 1])
def test_git_discovery_reaps_child_when_drain_thread_is_refused(monkeypatch, allowed):
    from sonder_runtime.adapters import git_discovery

    spawned = _recording_popen(monkeypatch, git_discovery.subprocess)
    _refuse_after(monkeypatch, git_discovery, allowed)
    with pytest.raises(ThreadOwnershipRefused):
        git_discovery._run_bounded(_SLEEPER, timeout_seconds=30, output_limit=1024)
    _assert_reaped(spawned[0])


def test_storage_probe_reaps_child_when_reader_is_refused(monkeypatch, tmp_path):
    from sonder_runtime.adapters import storage

    monkeypatch.setattr(storage, "_PROBE_WORKER", "import time; time.sleep(60)")
    spawned = _recording_popen(monkeypatch, storage.subprocess)
    _refuse_after(monkeypatch, storage, 0)
    with pytest.raises(ThreadOwnershipRefused):
        storage.throughput_probe(tmp_path)
    _assert_reaped(spawned[0])


@pytest.mark.parametrize("allowed", [0, 1])
def test_npu_worker_reaps_child_when_pump_is_refused(monkeypatch, allowed):
    from sonder_runtime.adapters.accelerators.npu import npu_broker

    proc = subprocess.Popen(
        _SLEEPER, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _refuse_after(monkeypatch, npu_broker, allowed)
    with pytest.raises(ThreadOwnershipRefused):
        npu_broker._Worker(proc, 1)
    _assert_reaped(proc)


def test_npu_warmup_refusal_does_not_stick_in_warming(monkeypatch):
    from sonder_runtime.adapters.accelerators.npu import npu_broker

    broker = npu_broker.NpuBroker()
    monkeypatch.setattr(broker, "_ram_gate_reason", lambda manifests: None)
    _refuse_after(monkeypatch, npu_broker, 0)
    assert broker.ensure_warm([]) is False
    # Otherwise every later ensure_warm() answers "warming" forever and no
    # warmup is ever attempted again.
    assert broker._state == "cold"
    assert broker._warm_thread is None


def test_npu_warmup_spawn_refusal_returns_broker_to_cold(monkeypatch):
    from sonder_runtime.adapters.accelerators.npu import npu_broker

    broker = npu_broker.NpuBroker()

    def refused_spawn(manifests=None):
        raise ThreadOwnershipRefused("runtime thread capacity exhausted")

    monkeypatch.setattr(broker, "_spawn_worker", refused_spawn)
    with broker._state_lock:
        broker._state = "warming"
        broker._generation += 1
        generation = broker._generation
    broker._warmup_exclusive(generation, {})
    assert broker._state == "cold"


def test_process_job_output_persister_finishes_when_reader_is_refused(monkeypatch):
    from sonder_runtime.adapters.execution import process_jobs

    provider = object.__new__(process_jobs.SubprocessJobProvider)
    provider._output_batch = None
    provider._timer_lock = threading.RLock()
    provider._output_batchers = {}
    provider._output_threads = {}
    provider._persist_output = lambda job_id, batch: None
    provider._persistence_stopped = lambda job_id, exc: None
    provider._remember_output_failure = lambda job_id, exc: None

    class _Process:
        stdout = io.StringIO("")
        stderr = io.StringIO("")

    # Persister and the stdout reader are admitted; the stderr reader is not.
    _refuse_after(monkeypatch, process_jobs, 2)
    with pytest.raises(ThreadOwnershipRefused):
        provider._start_output_readers("job-1", _Process())
    persister = provider._output_threads["job-1"][0]
    persister.join(timeout=5)
    # A persister that waits for a writer which was never created holds an
    # owned thread slot for the life of the process.
    assert not persister.is_alive()


def test_extension_host_reaps_child_when_reader_is_refused(monkeypatch):
    from sonder_runtime.adapters.extensions import host as extension_host

    spawned = _recording_popen(monkeypatch, extension_host.subprocess)
    host = extension_host.ExtensionHost(
        _SLEEPER, popen=extension_host.subprocess.Popen,
    )
    _refuse_after(monkeypatch, extension_host, 0)
    with pytest.raises(extension_host.ExtensionHostError):
        host.start()
    # A live child with no completed handshake must not be kept: start()
    # would later accept it as ready.
    assert host._process is None
    _assert_reaped(spawned[0])


def test_mcp_provider_reaps_child_and_clears_active_when_worker_is_refused(monkeypatch):
    from sonder_runtime.adapters import mcp_subprocess

    spawned = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    provider = mcp_subprocess.McpSubprocessProvider(_SLEEPER, popen=popen)
    _refuse_after(monkeypatch, mcp_subprocess, 0)
    with pytest.raises(ThreadOwnershipRefused):
        provider.run("{}\n")
    _assert_reaped(spawned[0])
    # Otherwise every later exchange is refused as "already active".
    assert provider._process is None
