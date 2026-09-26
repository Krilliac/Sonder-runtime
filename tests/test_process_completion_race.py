"""Regression coverage for cancellation racing process completion."""

from __future__ import annotations

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
from sonder_runtime.application.ports.jobs import (
    TERMINAL_JOB_STATUSES,
    JobIdentity,
    JobStatus,
)


class _ExitedProcess:
    pid = 4242

    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code

    def wait(self, timeout=None):
        return self.exit_code


class _Cleanup:
    def __init__(self, complete: bool) -> None:
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


def _request(job_id: str) -> ProcessJobRequest:
    return ProcessJobRequest(
        JobIdentity(job_id, "process", "execute", f"idem-{job_id}"),
        ("python", "-c", "pass"),
        cwd=Path.cwd(),
    )


def _registry(kind: str, tmp_path: Path, job_id: str):
    if kind == "memory":
        return DurableJobRegistry(), None
    path = tmp_path / f"{job_id}.sqlite"
    return SQLiteDurableJobRegistry(path), path


def _provider(registry, cleanup, *, timers, max_concurrent_processes=1):
    def timer_factory(delay, callback, args=()):
        timer = _Timer(delay, callback, args)
        timers.append(timer)
        return timer

    return SubprocessJobProvider(
        registry,
        process_cleanup=cleanup,
        launcher=lambda *args, **kwargs: _ExitedProcess(1),
        memory_limiter=_MemoryLimiter(),
        process_identity_resolver=lambda _pid: "owned-process",
        platform_name="posix",
        max_concurrent_processes=max_concurrent_processes,
        timer_factory=timer_factory,
    )


def _install_before_transition_cancellation(registry, cancel_registry):
    original = registry.transition
    injected = False

    def transition(job_id, status, *args, **kwargs):
        nonlocal injected
        if status in TERMINAL_JOB_STATUSES and not injected:
            injected = True
            cancel_registry.request_cancellation(job_id, reason="external cancellation won")
        return original(job_id, status, *args, **kwargs)

    registry.transition = transition


def _install_after_transition_cancellation(registry, cancel_registry):
    original = registry.transition
    injected = False

    def transition(job_id, status, *args, **kwargs):
        nonlocal injected
        result = original(job_id, status, *args, **kwargs)
        if status in TERMINAL_JOB_STATUSES and not injected:
            injected = True
            cancel_registry.request_cancellation(job_id, reason="late cancellation")
        return result

    registry.transition = transition


@pytest.mark.parametrize("kind", ("memory", "sqlite"))
@pytest.mark.parametrize("exit_code", (0, 3))
@pytest.mark.parametrize("cleanup_complete", (False, True))
def test_completion_cas_preserves_cancellation_and_cleanup_contract(
    tmp_path, kind, exit_code, cleanup_complete
):
    registry, path = _registry(kind, tmp_path, f"race-{kind}-{exit_code}-{cleanup_complete}")
    cancel_registry = registry if path is None else SQLiteDurableJobRegistry(path)
    cleanup = _Cleanup(cleanup_complete)
    timers = []
    provider = _provider(registry, cleanup, timers=timers)
    request = _request(f"race-{kind}-{exit_code}-{cleanup_complete}")
    provider._launcher = lambda *args, **kwargs: _ExitedProcess(exit_code)
    started = provider.start(request)
    _install_before_transition_cancellation(registry, cancel_registry)

    waited = provider.wait(started.record.identity.job_id)

    assert cleanup.requests
    assert cleanup.requests[0].process_identity == "owned-process"
    assert cancel_registry.poll(request.identity.job_id).status is waited.record.status
    if cleanup_complete:
        assert waited.record.status is JobStatus.CANCELLED
        assert started.record.identity.job_id not in provider._processes
    else:
        assert waited.record.status is JobStatus.CANCELLATION_REQUESTED
        assert started.record.identity.job_id in provider._processes
        assert timers and timers[-1].started
        with pytest.raises(RuntimeError, match="capacity exhausted"):
            provider.start(_request("blocked-after-race"))


@pytest.mark.parametrize("kind", ("memory", "sqlite"))
@pytest.mark.parametrize("exit_code,expected", ((0, JobStatus.SUCCEEDED), (3, JobStatus.FAILED)))
def test_completion_wins_when_cancellation_is_late(tmp_path, kind, exit_code, expected):
    registry, path = _registry(kind, tmp_path, f"late-{kind}-{exit_code}")
    cancel_registry = registry if path is None else SQLiteDurableJobRegistry(path)
    provider = _provider(registry, _Cleanup(True), timers=[])
    provider._launcher = lambda *args, **kwargs: _ExitedProcess(exit_code)
    request = _request(f"late-{kind}-{exit_code}")
    started = provider.start(request)
    _install_after_transition_cancellation(registry, cancel_registry)

    waited = provider.wait(started.record.identity.job_id)

    assert waited.record.status is expected
    assert cancel_registry.poll(request.identity.job_id).status is expected


def test_sqlite_completion_cas_detects_cancellation_after_its_row_read(tmp_path):
    registry, path = _registry("sqlite", tmp_path, "inside-sqlite-cas")
    other = SQLiteDurableJobRegistry(path)
    request = _request("inside-sqlite-cas")
    current = registry.start(request.identity)
    original_clock = registry._clock

    def cancel_before_update():
        other.request_cancellation(request.identity.job_id, reason="after row read")
        return original_clock()

    registry._clock = cancel_before_update
    result = registry.transition(
        request.identity.job_id, JobStatus.SUCCEEDED,
        expected_revision=current.revision, expected_status=current.status,
    )

    assert result.status is JobStatus.CANCELLATION_REQUESTED
    assert result.error == "after row read"
    assert other.poll(request.identity.job_id).status is JobStatus.CANCELLATION_REQUESTED


def test_nonterminal_cas_conflict_retains_output_failure_for_retry():
    registry = DurableJobRegistry()
    provider = _provider(registry, _Cleanup(True), timers=[])
    provider._launcher = lambda *args, **kwargs: _ExitedProcess(0)
    request = _request("paused-completion")
    provider.start(request)
    provider._remember_output_failure(request.identity.job_id, OSError("output unavailable"))
    original = registry.transition

    def pause_before_completion(job_id, status, **kwargs):
        registry.transition = original
        original(job_id, JobStatus.PAUSED)
        return original(job_id, status, **kwargs)

    registry.transition = pause_before_completion
    first = provider.wait(request.identity.job_id)
    assert first.record.status is JobStatus.PAUSED
    assert request.identity.job_id in provider._processes

    second = provider.wait(request.identity.job_id)
    assert second.record.status is JobStatus.FAILED
    assert "output persistence failed" in second.record.error
