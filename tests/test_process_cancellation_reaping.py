import os
import subprocess
import sys
from pathlib import Path

import pytest

from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
from sonder_runtime.adapters.persistence.sqlite.job_registry import (
    SQLiteDurableJobRegistry,
)
from sonder_runtime.application.execution.process_jobs import ProcessJobRequest
from sonder_runtime.application.jobs.durable_registry import (
    DurableJobRegistry,
    ProcessTreeCleanupReceipt,
)
from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus


class _ExitedProcess:
    pid = 4242

    def __init__(self, exit_code):
        self.exit_code = exit_code
        self.wait_calls = []

    def poll(self):
        return self.exit_code

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.exit_code


class _Cleanup:
    def __init__(self, complete):
        self.complete = complete
        self.requests = []

    def cleanup(self, request):
        self.requests.append(request)
        return ProcessTreeCleanupReceipt(
            request.job_id,
            True,
            descendants_seen=1,
            descendants_terminated=1 if self.complete else 0,
            complete=self.complete,
            detail="tree proven" if self.complete else "tree remains",
        )


class _MemoryLimiter:
    def apply(self, process, limit_bytes):
        return None


class _Timer:
    def __init__(self, delay, callback, args):
        self.delay = delay
        self.callback = callback
        self.args = args
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


def _request(job_id):
    return ProcessJobRequest(
        JobIdentity(job_id, "process", "execute", f"idem-{job_id}"),
        ("python", "-c", "pass"),
        cwd=Path.cwd(),
    )


def _provider(process, cleanup, *, max_concurrent_processes=None, platform_name="posix"):
    timers = []

    def timer_factory(delay, callback, args=()):
        timer = _Timer(delay, callback, args)
        timers.append(timer)
        return timer

    provider = SubprocessJobProvider(
        DurableJobRegistry(),
        process_cleanup=cleanup,
        launcher=lambda *args, **kwargs: process,
        memory_limiter=_MemoryLimiter(),
        process_identity_resolver=lambda _pid: "owned-process",
        platform_name=platform_name,
        max_concurrent_processes=max_concurrent_processes,
        timer_factory=timer_factory,
    )
    provider._test_timers = timers
    return provider


def test_wait_preserves_durable_cancellation_until_cleanup_proof():
    process = _ExitedProcess(exit_code=1)
    cleanup = _Cleanup(complete=True)
    provider = _provider(process, cleanup)
    started = provider.start(_request("cancelled-wait"))
    provider._registry.request_cancellation(
        started.record.identity.job_id, reason="deadline exceeded"
    )

    waited = provider.wait(started.record.identity.job_id)

    assert waited.record.status is JobStatus.CANCELLED
    assert cleanup.requests
    assert cleanup.requests[0].process_identity == "owned-process"
    assert cleanup.requests[0].process_group_id == process.pid


def test_incomplete_cancellation_cleanup_stays_retryable_and_retains_process():
    process = _ExitedProcess(exit_code=1)
    cleanup = _Cleanup(complete=False)
    provider = _provider(process, cleanup)
    started = provider.start(_request("incomplete-cancel"))
    provider._registry.request_cancellation(
        started.record.identity.job_id, reason="cancel requested"
    )

    waited = provider.wait(started.record.identity.job_id)

    assert waited.record.status is JobStatus.CANCELLATION_REQUESTED
    assert started.record.identity.job_id in provider._processes
    assert len(cleanup.requests) == 1


def test_incomplete_cleanup_retries_and_releases_capacity_after_proof():
    process = _ExitedProcess(exit_code=1)
    cleanup = _Cleanup(complete=False)
    provider = _provider(process, cleanup, max_concurrent_processes=1)
    started = provider.start(_request("retry-cancel"))
    provider._registry.request_cancellation(
        started.record.identity.job_id, reason="cancel requested"
    )

    first = provider.wait(started.record.identity.job_id)
    assert first.record.status is JobStatus.CANCELLATION_REQUESTED
    assert provider._test_timers and provider._test_timers[-1].started
    with pytest.raises(RuntimeError, match="capacity exhausted"):
        provider.start(_request("blocked-by-retry"))

    cleanup.complete = True
    second = provider.cancel(started.record.identity.job_id, reason="retry cleanup")

    assert second.cleanup_completed
    assert second.records[-1].status is JobStatus.CANCELLED
    assert provider._test_timers[-1].cancelled
    provider.start(_request("after-retry"))


def test_deadline_retry_keeps_pending_cancellation_after_root_exit():
    process = _ExitedProcess(exit_code=1)
    cleanup = _Cleanup(complete=False)
    provider = _provider(process, cleanup, max_concurrent_processes=1)
    started = provider.start(_request("deadline-retry-exited"))
    job_id = started.record.identity.job_id
    provider._registry.request_cancellation(job_id, reason="operator cancellation")
    provider._discard_deadline(job_id)
    timers_before = len(provider._test_timers)

    provider._expire_deadline_owned(job_id)

    assert provider._registry.poll(job_id).status is JobStatus.CANCELLATION_REQUESTED
    assert len(cleanup.requests) == 1
    assert job_id in provider._processes
    assert len(provider._test_timers) > timers_before
    assert provider._test_timers[-1].started
    assert provider._test_timers[-1].args == (job_id,)
    with pytest.raises(RuntimeError, match="capacity exhausted"):
        provider.start(_request("blocked-by-pending-cancellation"))


def test_restarted_deadline_retry_keeps_pending_cancellation_after_root_exit():
    from sonder_runtime.adapters.process_liveness import PROCESS_DEAD

    registry = DurableJobRegistry()
    cleanup = _Cleanup(complete=False)
    first = SubprocessJobProvider(
        registry,
        process_cleanup=cleanup,
        launcher=lambda *args, **kwargs: _ExitedProcess(exit_code=1),
        memory_limiter=_MemoryLimiter(),
        process_identity_resolver=lambda _pid: "owned-process",
        platform_name="posix",
        timer_factory=lambda delay, callback, args=(): _Timer(delay, callback, args),
    )
    job_id = first.start(_request("restarted-retry")).record.identity.job_id
    registry.request_cancellation(job_id, reason="operator cancellation")
    first._discard_deadline(job_id)

    # A restarted owner has no process handle; the recorded root has exited.
    timers = []
    second = SubprocessJobProvider(
        registry,
        process_cleanup=cleanup,
        memory_limiter=_MemoryLimiter(),
        process_identity_resolver=lambda _pid: "owned-process",
        process_probe=lambda _pid, _expected: (PROCESS_DEAD, None),
        platform_name="posix",
        timer_factory=lambda delay, callback, args=(): timers.append(
            _Timer(delay, callback, args)) or timers[-1],
    )
    second._discard_deadline(job_id)
    timers.clear()

    second._expire_deadline_owned(job_id)

    record = registry.poll(job_id)
    assert record.status is JobStatus.CANCELLATION_REQUESTED
    assert cleanup.requests
    assert cleanup.requests[-1].process_identity == "owned-process"
    assert timers and timers[-1].started and timers[-1].args == (job_id,)


def test_windows_taskkill_success_does_not_release_unproven_job_capacity():
    from types import SimpleNamespace

    from sonder_runtime.adapters.process_liveness import PROCESS_ALIVE
    from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor

    calls = []
    subprocess_api = SimpleNamespace(
        DEVNULL=subprocess.DEVNULL,
        SubprocessError=subprocess.SubprocessError,
        run=lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0),
    )
    cleanup = ProcessTreeSupervisor(
        platform_name="nt",
        subprocess_module=subprocess_api,
        process_probe=lambda _pid, identity: (PROCESS_ALIVE, identity),
    )
    process = _ExitedProcess(exit_code=1)
    provider = _provider(
        process, cleanup, max_concurrent_processes=1, platform_name="nt"
    )
    started = provider.start(_request("windows-unproven"))
    job_id = started.record.identity.job_id
    provider._registry.request_cancellation(job_id, reason="operator cancellation")

    waited = provider.wait(job_id)

    assert waited.record.status is JobStatus.CANCELLATION_REQUESTED
    assert calls == [["taskkill", "/PID", str(process.pid), "/T", "/F"]]
    assert job_id in provider._processes
    assert provider._test_timers[-1].started
    with pytest.raises(RuntimeError, match="capacity exhausted"):
        provider.start(_request("blocked-by-unproven-windows-tree"))


@pytest.mark.skipif(os.name != "posix", reason="requires native POSIX process groups")
@pytest.mark.parametrize("exit_code", (0, 3))
def test_real_posix_cancelled_child_reaps_after_recorded_exit(tmp_path, exit_code):
    from sonder_runtime.adapters.process_liveness import process_identity
    from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor

    release = tmp_path / "release"
    script = """
import pathlib
import sys
import time
p = pathlib.Path(sys.argv[1])
while not p.exists():
    time.sleep(0.01)
raise SystemExit(int(sys.argv[2]))
"""
    cleanup = ProcessTreeSupervisor(platform_name="posix", timeout_seconds=2)
    timers = []

    def timer_factory(delay, callback, args=()):
        timer = _Timer(delay, callback, args)
        timers.append(timer)
        return timer

    registry_path = tmp_path / "durable-jobs.sqlite"
    registry = SQLiteDurableJobRegistry(registry_path)
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=cleanup,
        memory_limiter=_MemoryLimiter(),
        platform_name="posix",
        process_identity_resolver=process_identity,
        timer_factory=timer_factory,
    )
    request = _request("real-cancel")
    request = ProcessJobRequest(
        request.identity,
        (sys.executable, "-c", script, str(release), str(exit_code)),
        cwd=tmp_path,
    )
    try:
        started = provider.start(request)
        persisted = SQLiteDurableJobRegistry(registry_path).view(request.identity.job_id)
        assert persisted.process_id == started.process_id
        assert persisted.metadata["process_instance_identity"]
        provider._registry.request_cancellation(
            started.record.identity.job_id, reason="release then cancel"
        )
        release.touch()
        waited = provider.wait(started.record.identity.job_id, timeout=5)
        assert waited.record.status is JobStatus.CANCELLED
        assert waited.exit_code == exit_code
        reopened = SQLiteDurableJobRegistry(registry_path).poll(request.identity.job_id)
        assert reopened.status is JobStatus.CANCELLED
    finally:
        process = provider._processes.get(request.identity.job_id)
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass


def test_natural_exit_without_cancellation_keeps_exit_status_mapping():
    for exit_code, expected in ((0, JobStatus.SUCCEEDED), (3, JobStatus.FAILED)):
        process = _ExitedProcess(exit_code=exit_code)
        provider = _provider(process, _Cleanup(complete=True))
        started = provider.start(_request(f"natural-{exit_code}"))

        waited = provider.wait(started.record.identity.job_id)

        assert waited.record.status is expected
