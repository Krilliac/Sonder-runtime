from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
from sonder_runtime.adapters.extensions.memory_limits import (
    PreparedProcessContainment,
    ProcessContainmentResult,
)
from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor
from sonder_runtime.application.execution.process_jobs import ProcessJobRequest
from sonder_runtime.application.jobs.durable_registry import (
    DurableJobRegistry,
    ProcessTreeCleanupReceipt,
)
from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus
from sonder_runtime.adapters.process_liveness import PROCESS_ALIVE


def _request(job_id: str = "job-process") -> ProcessJobRequest:
    return ProcessJobRequest(
        JobIdentity(job_id, "process", "execute", f"idem-{job_id}"),
        ("python", "-c", "pass"),
        cwd=Path("C:/workspace"),
        environment=(("SONDER_TEST", "1"),),
        max_descendants=9,
    )


@pytest.mark.parametrize("failure", ["prepare", "registry", "launch", "identity", "attach", "limits", "readers", "already_released"])
def test_failed_start_returns_single_capacity_slot(failure, monkeypatch):
    from dataclasses import replace
    registry = DurableJobRegistry()
    process = _Process()
    provider = SubprocessJobProvider(
        registry, process_cleanup=_Cleanup(complete=True),
        launcher=lambda *args, **kwargs: process, memory_limiter=_MemoryLimiter(),
        max_concurrent_processes=1, process_identity_resolver=lambda pid: "stable",
        platform_name="posix",
    )
    def fail(*args, **kwargs):
        raise RuntimeError("injected failed start")
    bad = _request("failed-start")
    with monkeypatch.context() as patch:
        if failure == "prepare":
            bad = replace(bad, require_job_scope=True)
        elif failure == "registry":
            patch.setattr(registry, "start", fail)
        elif failure == "launch":
            patch.setattr(provider, "_launcher", fail)
        elif failure == "identity":
            patch.setattr(provider, "_process_identity_resolver", fail)
        elif failure == "attach":
            patch.setattr(registry, "attach_process", fail)
        elif failure == "limits":
            bad = replace(bad, memory_limit_bytes=1024)
            patch.setattr(provider._memory_limiter, "apply", fail)
        elif failure == "readers":
            patch.setattr(provider, "_start_output_readers", fail)
        elif failure == "already_released":
            def release_then_fail(job_id, process):
                provider._forget_local_job(job_id)
                fail()
            patch.setattr(provider, "_start_output_readers", release_then_fail)
        with pytest.raises(RuntimeError):
            provider.start(bad)
    provider.start(_request("valid-after-failure"))
    with pytest.raises(RuntimeError, match="capacity exhausted"):
        provider.start(_request("cannot-overbook"))
    provider.wait("valid-after-failure")
    provider.start(_request("valid-after-completion"))
    provider.cancel("valid-after-completion")
    provider.start(_request("valid-after-cancellation"))
    provider.wait("valid-after-cancellation")


def test_process_publication_already_has_releasable_capacity_lease():
    provider = SubprocessJobProvider(
        DurableJobRegistry(), process_cleanup=_Cleanup(complete=True),
        launcher=lambda *args, **kwargs: _Process(), memory_limiter=_MemoryLimiter(),
        max_concurrent_processes=1, process_identity_resolver=lambda pid: "stable",
        platform_name="posix",
    )
    completed = []

    class FinishAtPublication(dict):
        def __setitem__(self, job_id, process):
            super().__setitem__(job_id, process)
            if job_id == "finish-at-publication":
                # Deterministically yield to a consumer at first visibility,
                # before the publishing assignment returns to start().
                completed.append(provider.wait(job_id).record.status)

    provider._processes = FinishAtPublication()
    provider.start(_request("finish-at-publication"))
    assert completed == [JobStatus.SUCCEEDED]
    assert "finish-at-publication" not in provider._process_slot_owners
    provider.start(_request("after-publication-race"))
    with pytest.raises(RuntimeError, match="capacity exhausted"):
        provider.start(_request("no-overbooking"))
    provider.wait("after-publication-race")


@pytest.mark.parametrize("cleanup_raises", [False, True])
def test_failed_launch_retains_capacity_until_exit_and_containment_proven(cleanup_raises, monkeypatch):
    from dataclasses import replace

    class LiveProcess(_Process):
        exited = False

        def wait(self, timeout=None):
            if not self.exited:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return 0

    process = LiveProcess()
    token = _ScopedToken(ProcessContainmentResult(False), ProcessContainmentResult(True))
    provider = SubprocessJobProvider(
        DurableJobRegistry(), process_cleanup=_Cleanup(complete=True),
        launcher=lambda *args, **kwargs: process,
        memory_limiter=_ScopedLimiter(token), max_concurrent_processes=1,
        process_identity_resolver=lambda pid: "identity", platform_name="posix",
    )
    retries = []
    monkeypatch.setattr(provider, "_schedule_deadline", lambda *args: retries.append(args))
    def fail(*args, **kwargs):
        raise RuntimeError("injected identity/cleanup failure")
    with monkeypatch.context() as patch:
        patch.setattr(provider, "_process_identity_resolver", fail)
        if cleanup_raises:
            patch.setattr(provider, "_quiesce_containment", fail)
        with pytest.raises(RuntimeError):
            provider.start(replace(_request("unresolved-launch"), require_job_scope=True))
    assert provider._processes["unresolved-launch"] is process
    with pytest.raises(RuntimeError, match="capacity exhausted"):
        provider.start(_request("blocked-until-clean"))
    # Containment becoming empty alone cannot prove the root process exited.
    assert not provider.cancel("unresolved-launch").cleanup_completed
    with pytest.raises(RuntimeError, match="capacity exhausted"):
        provider.start(_request("still-blocked"))
    process.exited = True
    assert provider.cancel("unresolved-launch").cleanup_completed
    assert "unresolved-launch" not in provider._process_slot_owners
    provider.start(_request("after-proven-cleanup"))
    provider.wait("after-proven-cleanup")


def test_failed_launch_retains_worker_reservation_until_root_exit(tmp_path, monkeypatch):
    from dataclasses import replace
    from sonder_runtime.application.compute_fabric.capacity import WorkerBudget
    from sonder_runtime.application.errors import CapacityExceeded

    class LiveProcess(_Process):
        exited = False

        def wait(self, timeout=None):
            if not self.exited:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return 0

    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    budget = WorkerBudget("host", 100, 1)
    reservation = registry.reserve_capacity(budget, "failed-worker-launch", "a" * 64, 100)
    process = LiveProcess()
    provider = SubprocessJobProvider(
        registry, process_cleanup=_Cleanup(complete=True),
        launcher=lambda *args, **kwargs: process,
        memory_limiter=_ScopedLimiter(_ScopedToken(ProcessContainmentResult(True))),
        process_identity_resolver=lambda pid: "identity", platform_name="posix",
    )
    monkeypatch.setattr(provider, "_schedule_deadline", lambda *args: None)
    def fail(*args):
        raise RuntimeError("identity lookup failed")
    with monkeypatch.context() as patch:
        patch.setattr(provider, "_process_identity_resolver", fail)
        with pytest.raises(RuntimeError, match="identity lookup failed"):
            provider.start(replace(_request("failed-worker-launch"),
                                   require_job_scope=True, capacity_token=reservation.token))
    with pytest.raises(CapacityExceeded):
        registry.reserve_capacity(budget, "second", "b" * 64, 100)
    process.exited = True
    assert provider.cancel("failed-worker-launch").cleanup_completed
    assert registry.reserve_capacity(budget, "second", "b" * 64, 100)


class _Process:
    pid = 77

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.killed = False
        self.wait_calls: list[float | None] = []

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.exit_code

    def kill(self):
        self.killed = True


class _Cleanup:
    def __init__(self, *, complete: bool) -> None:
        self.complete = complete
        self.requests = []

    def cleanup(self, request):
        self.requests.append(request)
        return ProcessTreeCleanupReceipt(
            request.job_id,
            True,
            descendants_seen=2,
            descendants_terminated=2 if self.complete else 1,
            complete=self.complete,
            detail="tree clean" if self.complete else "descendant remains",
        )


class _LimitToken:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _MemoryLimiter:
    def __init__(self, *, fail=False):
        self.calls = []
        self.token = _LimitToken()
        self.fail = fail

    def apply(self, process, limit_bytes):
        self.calls.append((process, limit_bytes))
        if self.fail:
            raise RuntimeError("limit unavailable")
        return self.token


class _ContainmentLimiter:
    def __init__(self) -> None:
        self.events = []
        self.token = _LimitToken()

    def launch_options(self, memory_limit_bytes, process_limit):
        self.events.append(("prepare", memory_limit_bytes, process_limit))
        return {"containment_marker": "prepared"}

    def apply_process_limits(self, process, memory_limit_bytes, process_limit):
        self.events.append(("attach", process.pid, memory_limit_bytes, process_limit))
        return self.token

    def resume(self, process):
        self.events.append(("resume", process.pid))

    def apply(self, process, limit_bytes):
        raise AssertionError("post-launch fallback must not be used")


class _ScopedToken(_LimitToken):
    def __init__(self, *results: ProcessContainmentResult) -> None:
        super().__init__()
        self.results = list(results)
        self.calls: list[bool] = []

    def quiesce(self, *, force: bool) -> ProcessContainmentResult:
        self.calls.append(force)
        if self.results:
            return self.results.pop(0)
        return ProcessContainmentResult(True)


class _ScopedLimiter:
    def __init__(self, token: _ScopedToken) -> None:
        self.token = token
        self.calls = []
        self.restore_calls = []

    def prepare_process_job(self, job_id, argv, memory_limit_bytes, process_limit):
        self.calls.append((job_id, argv, memory_limit_bytes, process_limit))
        return PreparedProcessContainment(
            argv=("systemd-run", "--scope", *argv),
            launch_options={"scope_marker": job_id},
            token=self.token,
        )

    def apply(self, process, limit_bytes):
        raise AssertionError("scoped process jobs must not use the fallback limiter")

    def restore_process_job(self, job_id, metadata):
        self.restore_calls.append((job_id, dict(metadata)))
        return self.token


def _provider(cleanup, process, *, platform_name="posix", launch_options=None):
    options = launch_options if launch_options is not None else {}

    def launch(argv, **kwargs):
        options.update(argv=argv, kwargs=kwargs)
        return process

    return SubprocessJobProvider(
        DurableJobRegistry(),
        process_cleanup=cleanup,
        launcher=launch,
        platform_name=platform_name,
        memory_limiter=_MemoryLimiter(),
    ), options


def test_start_registers_process_identity_and_posix_group_for_cleanup():
    cleanup = _Cleanup(complete=True)
    provider, launch = _provider(cleanup, _Process())

    started = provider.start(_request())

    assert started.record.identity.job_id == "job-process"
    assert started.process_id == 77
    assert started.process_group_id == 77
    assert launch["kwargs"]["start_new_session"] is True
    assert launch["kwargs"]["env"]["SONDER_TEST"] == "1"
    assert launch["kwargs"]["cwd"] == str(Path("C:/workspace"))


def test_start_scrubs_parent_control_secrets_before_request_overlay(monkeypatch):
    monkeypatch.setenv("SONDER_API_KEY", "host-owner-secret")
    monkeypatch.setenv("SONDER_OPENAI_API_KEY", "host-cloud-secret")
    cleanup = _Cleanup(complete=True)
    provider, launch = _provider(cleanup, _Process())

    provider.start(_request())

    environment = launch["kwargs"]["env"]
    assert "SONDER_API_KEY" not in environment
    assert "SONDER_OPENAI_API_KEY" not in environment
    assert environment["SONDER_TEST"] == "1"


def test_memory_limit_is_enforced_before_job_publication_and_token_is_closed():
    cleanup = _Cleanup(complete=True)
    limiter = _MemoryLimiter()
    process = _Process()
    registry = DurableJobRegistry()
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=cleanup,
        launcher=lambda _argv, **_kwargs: process,
        platform_name="posix",
        memory_limiter=limiter,
    )
    request = _request()
    request = ProcessJobRequest(
        request.identity,
        request.argv,
        cwd=request.cwd,
        memory_limit_bytes=256 * 1024 * 1024,
    )
    provider.start(request)
    assert limiter.calls == [(process, 256 * 1024 * 1024)]
    provider.wait(request.identity.job_id)
    assert limiter.token.closed is True


def test_native_limits_are_prepared_before_launch_and_resume_after_attachment():
    cleanup = _Cleanup(complete=True)
    limiter = _ContainmentLimiter()
    process = _Process()
    launch = {}

    def launcher(argv, **kwargs):
        launch.update(kwargs)
        limiter.events.append(("launch", process.pid))
        return process

    registry = DurableJobRegistry()
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=cleanup,
        launcher=launcher,
        platform_name="posix",
        memory_limiter=limiter,
    )
    request = ProcessJobRequest(
        JobIdentity("job-contained", "process", "execute", "idem-contained"),
        ("python", "-c", "pass"),
        max_descendants=3,
        memory_limit_bytes=128 * 1024 * 1024,
    )
    provider.start(request)

    assert launch["containment_marker"] == "prepared"
    assert limiter.events == [
        ("prepare", 128 * 1024 * 1024, 4),
        ("launch", 77),
        ("attach", 77, 128 * 1024 * 1024, 4),
        ("resume", 77),
    ]
    assert registry.view("job-contained").process_id == 77


def test_strong_scope_must_be_quiescent_before_terminal_success():
    cleanup = _Cleanup(complete=True)
    token = _ScopedToken(
        ProcessContainmentResult(False, detail="scope still populated"),
        ProcessContainmentResult(True, forced=True, detail="scope killed"),
    )
    limiter = _ScopedLimiter(token)
    process = _Process()
    launch = {}
    provider = SubprocessJobProvider(
        DurableJobRegistry(),
        process_cleanup=cleanup,
        launcher=lambda argv, **kwargs: launch.update(argv=argv, kwargs=kwargs) or process,
        platform_name="posix",
        memory_limiter=limiter,
    )
    request = _request("job-scoped")
    request = ProcessJobRequest(
        request.identity,
        request.argv,
        cwd=request.cwd,
        max_descendants=request.max_descendants,
        require_job_scope=True,
    )

    provider.start(request)
    waited = provider.wait(request.identity.job_id)

    assert tuple(launch["argv"][:2]) == ("systemd-run", "--scope")
    assert waited.record.status is JobStatus.CANCELLATION_REQUESTED
    assert waited.record.is_terminal is False
    assert token.closed is False
    cancelled = provider.cancel(request.identity.job_id, "scope cleanup retry")
    assert cancelled.cleanup_completed is True
    assert cancelled.records[0].status is JobStatus.CANCELLED
    assert token.calls == [True, True]
    assert token.closed is True


def test_hard_deadline_watchdog_starts_before_process_launch():
    events = []
    timers = []

    class Timer:
        def __init__(self, delay, callback, args=()):
            self.delay = delay
            self.callback = callback
            self.args = args
            self.daemon = False
            timers.append(self)

        def start(self):
            events.append("timer-start")

        def cancel(self):
            events.append("timer-cancel")

    def launcher(_argv, **_kwargs):
        events.append("process-launch")
        return _Process()

    provider = SubprocessJobProvider(
        DurableJobRegistry(),
        process_cleanup=_Cleanup(complete=True),
        launcher=launcher,
        platform_name="posix",
        memory_limiter=_MemoryLimiter(),
        timer_factory=Timer,
        process_identity_resolver=lambda _pid: "birth-deadline-order",
    )
    request = _request("job-deadline-order")
    request = ProcessJobRequest(
        request.identity,
        request.argv,
        deadline_seconds=30,
    )

    provider.start(request)

    assert events[:2] == ["timer-start", "process-launch"]
    assert 0 < timers[0].delay <= 30
    provider._discard_deadline(request.identity.job_id)


def test_prelaunch_deadline_callback_prevents_scope_creation():
    launches = []

    class ImmediateTimer:
        def __init__(self, _delay, callback, args=()):
            self.callback = callback
            self.args = args
            self.daemon = False

        def start(self):
            self.callback(*self.args)

        def cancel(self):
            return None

    token = _ScopedToken(ProcessContainmentResult(True))
    provider = SubprocessJobProvider(
        DurableJobRegistry(),
        process_cleanup=_Cleanup(complete=True),
        launcher=lambda _argv, **_kwargs: launches.append(True) or _Process(),
        platform_name="posix",
        memory_limiter=_ScopedLimiter(token),
        timer_factory=ImmediateTimer,
    )
    request = ProcessJobRequest(
        JobIdentity("job-prelaunch-deadline", "compute-test", "controller", "idem-prelaunch"),
        ("python", "-c", "pass"),
        deadline_seconds=30,
        require_job_scope=True,
    )

    with pytest.raises(RuntimeError, match="deadline expired before launch"):
        provider.start(request)

    assert launches == []
    assert provider.poll(request.identity.job_id).status is JobStatus.CANCELLED


def test_timer_start_failure_cleans_reserved_scope_and_remains_discoverable():
    class BrokenTimer:
        def __init__(self, _delay, _callback, args=()):
            self.args = args
            self.daemon = False

        def start(self):
            raise RuntimeError("timer start failed")

        def cancel(self):
            return None

    token = _ScopedToken(ProcessContainmentResult(True))
    job_id = "job-timer-start-failure"
    provider = SubprocessJobProvider(
        DurableJobRegistry(),
        process_cleanup=_Cleanup(complete=True),
        launcher=lambda _argv, **_kwargs: (_ for _ in ()).throw(
            AssertionError("timer failure must prevent launch")
        ),
        platform_name="posix",
        memory_limiter=_ScopedLimiter(token),
        timer_factory=BrokenTimer,
    )
    request = ProcessJobRequest(
        JobIdentity(job_id, "compute-test", "controller", "idem-timer-failure"),
        ("python", "-c", "pass"),
        deadline_seconds=30,
        require_job_scope=True,
    )

    with pytest.raises(RuntimeError, match="timer start failed"):
        provider.start(request)

    assert provider.poll(job_id).status is JobStatus.FAILED
    assert job_id not in provider._memory_tokens
    assert job_id not in provider._launch_locks
    assert token.closed is True


def test_expired_deadline_rearms_cleanup_after_incomplete_attempt():
    timers = []

    class Timer:
        def __init__(self, delay, callback, args=()):
            self.delay = delay
            self.callback = callback
            self.args = args
            self.daemon = False
            timers.append(self)

        def start(self):
            return None

        def cancel(self):
            return None

    provider = SubprocessJobProvider(
        DurableJobRegistry(),
        process_cleanup=_Cleanup(complete=False),
        launcher=lambda _argv, **_kwargs: _Process(),
        platform_name="posix",
        memory_limiter=_MemoryLimiter(),
        timer_factory=Timer,
        cleanup_retry_seconds=0.25,
        process_identity_resolver=lambda _pid: "birth-deadline-retry",
    )
    request = _request("job-deadline-retry")
    request = ProcessJobRequest(request.identity, request.argv, deadline_seconds=30)
    provider.start(request)
    provider._expire_deadline(request.identity.job_id)

    assert provider.poll(request.identity.job_id).status is JobStatus.CANCELLATION_REQUESTED
    assert len(timers) == 2
    assert timers[-1].delay == pytest.approx(0.25)
    assert request.identity.job_id in provider._deadline_timers


def test_restarted_systemd_scope_is_reowned_before_invalid_metadata_cleanup():
    class Timer:
        def __init__(self, _delay, _callback, args=()):
            self.args = args
            self.daemon = False

        def start(self):
            return None

        def cancel(self):
            return None

    job_id = "job-restarted-invalid-scope"
    token = _ScopedToken(ProcessContainmentResult(True, forced=True))
    limiter = _ScopedLimiter(token)
    registry = DurableJobRegistry()
    registry.start(
        JobIdentity(job_id, "compute-test", "controller", "idem-restarted-scope"),
        metadata={
            "hard_deadline_at": "2099-01-01T00:00:00+00:00",
            "max_descendants": "0",
            "containment_kind": "systemd_scope",
            "containment_unit": "sonder-compute-0123456789abcdefabcd.scope",
            "containment_user": "1",
        },
    )

    provider = SubprocessJobProvider(
        registry,
        process_cleanup=_Cleanup(complete=False),
        launcher=lambda _argv, **_kwargs: _Process(),
        platform_name="posix",
        memory_limiter=limiter,
        timer_factory=Timer,
    )
    cancelled = provider.cancel(job_id, "invalid persisted safety metadata")

    assert cancelled.cleanup_completed is True
    assert cancelled.records[0].status is JobStatus.CANCELLED
    assert token.calls == [True]


def test_restarted_systemd_scope_without_deadline_fails_closed():
    class Timer:
        def __init__(self, _delay, _callback, args=()):
            self.args = args
            self.daemon = False

        def start(self):
            return None

        def cancel(self):
            return None

    job_id = "job-restarted-missing-deadline"
    limiter = _ScopedLimiter(_ScopedToken(ProcessContainmentResult(True, forced=True)))
    registry = DurableJobRegistry()
    registry.start(
        JobIdentity(job_id, "compute-test", "controller", "idem-missing-deadline"),
        metadata={
            "max_descendants": "4",
            "containment_kind": "systemd_scope",
            "containment_unit": "sonder-compute-0123456789abcdefabcd.scope",
            "containment_user": "1",
        },
    )

    provider = SubprocessJobProvider(
        registry,
        process_cleanup=_Cleanup(complete=False),
        launcher=lambda _argv, **_kwargs: _Process(),
        platform_name="posix",
        memory_limiter=limiter,
        timer_factory=Timer,
    )

    assert provider.poll(job_id).status is JobStatus.CANCELLATION_REQUESTED
    assert job_id in provider._deadline_timers


def test_launch_failure_stays_nonterminal_until_scope_is_empty():
    timers = []

    class Timer:
        def __init__(self, delay, callback, args=()):
            self.delay = delay
            self.callback = callback
            self.args = args
            self.daemon = False
            timers.append(self)

        def start(self):
            return None

        def cancel(self):
            return None

    job_id = "job-scope-launch-failure"
    token = _ScopedToken(
        ProcessContainmentResult(False, forced=True, detail="scope still populated"),
        ProcessContainmentResult(True, forced=True, detail="scope emptied"),
    )
    provider = SubprocessJobProvider(
        DurableJobRegistry(),
        process_cleanup=_Cleanup(complete=True),
        launcher=lambda _argv, **_kwargs: _Process(),
        platform_name="posix",
        memory_limiter=_ScopedLimiter(token),
        timer_factory=Timer,
        cleanup_retry_seconds=0.25,
        process_identity_resolver=lambda _pid: (_ for _ in ()).throw(
            RuntimeError("identity unavailable")
        ),
    )
    request = ProcessJobRequest(
        JobIdentity(job_id, "compute-test", "controller", "idem-launch-failure"),
        ("python", "-c", "pass"),
        deadline_seconds=30,
        require_job_scope=True,
    )

    with pytest.raises(RuntimeError, match="identity unavailable"):
        provider.start(request)

    assert provider.poll(job_id).status is JobStatus.CANCELLATION_REQUESTED
    assert job_id in provider._memory_tokens
    assert token.closed is False
    assert timers[-1].delay == pytest.approx(0.25)
    cancelled = provider.cancel(job_id, "retry launch cleanup")
    assert cancelled.cleanup_completed is True
    assert cancelled.records[0].status is JobStatus.CANCELLED


def test_restarted_reserved_scope_deadline_uses_restored_owner_and_safe_limit():
    class Timer:
        def __init__(self, _delay, _callback, args=()):
            self.args = args
            self.daemon = False

        def start(self):
            return None

        def cancel(self):
            return None

    job_id = "job-restarted-reserved-scope"
    token = _ScopedToken(ProcessContainmentResult(True, forced=True))
    limiter = _ScopedLimiter(token)
    registry = DurableJobRegistry()
    registry.start(
        JobIdentity(job_id, "compute-test", "controller", "idem-reserved-scope"),
        metadata={
            "hard_deadline_at": "2099-01-01T00:00:00+00:00",
            "max_descendants": "invalid",
            "launch_state": "reserved",
            "containment_kind": "systemd_scope",
            "containment_unit": "sonder-compute-0123456789abcdefabcd.scope",
            "containment_user": "1",
        },
    )
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=_Cleanup(complete=True),
        launcher=lambda _argv, **_kwargs: _Process(),
        platform_name="posix",
        memory_limiter=limiter,
        timer_factory=Timer,
    )

    provider._expire_deadline(job_id)

    assert provider.poll(job_id).status is JobStatus.CANCELLED
    assert token.calls == [True]


def test_unrestorable_scope_never_uses_generic_cleanup_as_terminal_proof():
    class Timer:
        def __init__(self, _delay, _callback, args=()):
            self.args = args
            self.daemon = False

        def start(self):
            return None

        def cancel(self):
            return None

    class BrokenScopeLimiter(_MemoryLimiter):
        def restore_process_job(self, _job_id, _metadata):
            raise RuntimeError("systemd unavailable")

    job_id = "job-unrestorable-scope"
    cleanup = _Cleanup(complete=True)
    registry = DurableJobRegistry()
    registry.start(
        JobIdentity(job_id, "compute-test", "controller", "idem-unrestorable"),
        process_id=77,
        process_group_id=77,
        metadata={
            "hard_deadline_at": "2099-01-01T00:00:00+00:00",
            "max_descendants": "4",
            "launch_state": "attached",
            "containment_kind": "systemd_scope",
            "containment_unit": "sonder-compute-0123456789abcdefabcd.scope",
            "containment_user": "1",
        },
    )
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=cleanup,
        launcher=lambda _argv, **_kwargs: _Process(),
        platform_name="posix",
        memory_limiter=BrokenScopeLimiter(),
        timer_factory=Timer,
    )

    cancelled = provider.cancel(job_id, "operator cancellation")

    assert cancelled.cleanup_completed is False
    assert provider.poll(job_id).status is JobStatus.CANCELLATION_REQUESTED
    assert cleanup.requests == []
    assert job_id in provider._deadline_timers


def test_direct_scope_cancellation_rearms_cleanup_retry():
    timers = []

    class Timer:
        def __init__(self, delay, callback, args=()):
            self.delay = delay
            self.callback = callback
            self.args = args
            self.daemon = False
            timers.append(self)

        def start(self):
            return None

        def cancel(self):
            return None

    job_id = "job-direct-cancel-retry"
    token = _ScopedToken(
        ProcessContainmentResult(False, forced=True, detail="scope still populated")
    )
    provider = SubprocessJobProvider(
        DurableJobRegistry(),
        process_cleanup=_Cleanup(complete=True),
        launcher=lambda _argv, **_kwargs: _Process(),
        platform_name="posix",
        memory_limiter=_ScopedLimiter(token),
        timer_factory=Timer,
        cleanup_retry_seconds=0.25,
        process_identity_resolver=lambda _pid: "birth-direct-cancel",
    )
    provider.start(ProcessJobRequest(
        JobIdentity(job_id, "compute-test", "controller", "idem-direct-cancel"),
        ("python", "-c", "pass"),
        deadline_seconds=30,
        require_job_scope=True,
    ))

    cancelled = provider.cancel(job_id, "operator cancellation")

    assert cancelled.cleanup_completed is False
    assert len(timers) == 2
    assert timers[-1].delay == pytest.approx(0.25)


def test_unenforceable_requested_memory_limit_aborts_before_registration():
    cleanup = _Cleanup(complete=True)
    limiter = _MemoryLimiter(fail=True)
    process = _Process()
    registry = DurableJobRegistry()
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=cleanup,
        launcher=lambda _argv, **_kwargs: process,
        platform_name="posix",
        memory_limiter=limiter,
    )
    request = ProcessJobRequest(
        JobIdentity("job-limit-fail", "process", "execute", "idem-limit-fail"),
        ("python", "-c", "pass"),
        memory_limit_bytes=128 * 1024 * 1024,
    )
    with pytest.raises(RuntimeError, match="limit unavailable"):
        provider.start(request)
    assert process.killed is True
    failed = registry.poll("job-limit-fail")
    assert failed.status is JobStatus.FAILED
    assert failed.error == "process launch failed (RuntimeError)"


def test_cancel_routes_the_concrete_job_through_typed_tree_cleanup():
    cleanup = _Cleanup(complete=True)
    process = _Process()
    provider, _ = _provider(cleanup, process)
    provider.start(_request())

    result = provider.cancel("job-process", "operator stop")

    assert result.cleanup_completed is True
    assert result.cleanup_receipts[0].complete is True
    assert cleanup.requests[0].process_id == 77
    assert cleanup.requests[0].process_group_id == 77
    assert cleanup.requests[0].max_descendants == 9
    assert cleanup.requests[0].reason == "operator stop"
    assert process.wait_calls == [0]


def test_provider_uses_the_typed_process_tree_supervisor_end_to_end():
    calls: list[tuple[int, int]] = []
    supervisor = ProcessTreeSupervisor(
        os_module=SimpleNamespace(
            name="posix",
            killpg=lambda group, signal: calls.append((group, signal)),
        ),
        signal_module=SimpleNamespace(SIGKILL=9),
        platform_name="posix",
    )
    process = _Process()
    provider = SubprocessJobProvider(
        DurableJobRegistry(),
        process_cleanup=supervisor,
        launcher=lambda _argv, **_kwargs: process,
        platform_name="posix",
        memory_limiter=_MemoryLimiter(),
    )
    provider.start(_request())

    result = provider.cancel("job-process", "typed supervisor")

    assert result.cleanup_completed is True
    assert calls == [(77, 9)]


def test_incomplete_cleanup_is_reported_and_live_mapping_is_retained():
    cleanup = _Cleanup(complete=False)
    process = _Process()
    provider, _ = _provider(cleanup, process)
    provider.start(_request())

    result = provider.cancel("job-process")

    assert result.cleanup_completed is False
    assert result.cleanup_receipts[0].complete is False
    assert result.detail == "descendant remains"
    assert result.records[0].status is JobStatus.CANCELLATION_REQUESTED
    assert result.records[0].is_terminal is False
    assert provider.wait("job-process", timeout=0).timed_out is False


def test_wait_publishes_terminal_truth_after_process_exit():
    cleanup = _Cleanup(complete=True)
    provider, _ = _provider(cleanup, _Process(exit_code=3))
    provider.start(_request())

    waited = provider.wait("job-process", timeout=2)

    assert waited.exit_code == 3
    assert waited.timed_out is False
    assert waited.record.status is JobStatus.FAILED
    assert waited.record.error == "process exited with a non-zero status"


def test_running_process_publishes_incremental_output_before_wait(tmp_path):
    cleanup = _Cleanup(complete=True)
    provider = SubprocessJobProvider(
        SQLiteDurableJobRegistry(tmp_path / "jobs.db"),
        process_cleanup=cleanup,
    )
    request = ProcessJobRequest(
        JobIdentity("job-live-output", "process", "execute", "idem-live-output"),
        (
            sys.executable, "-u", "-c",
            "import sys,time; print('first', flush=True); time.sleep(.25); print('second', flush=True)",
        ),
        max_descendants=4,
    )

    started = provider.start(request)
    deadline = time.monotonic() + 5
    page = provider._registry.stream(started.record.identity.job_id)
    while not page.events and time.monotonic() < deadline:
        time.sleep(.02)
        page = provider._registry.stream(started.record.identity.job_id)

    assert [event.data for event in page.events] == ["first\n"]
    assert provider._registry.poll("job-live-output").is_terminal is False

    waited = provider.wait("job-live-output", timeout=5)
    assert waited.record.status is JobStatus.SUCCEEDED
    tail = provider._registry.stream(
        "job-live-output", after=page.next_watermark,
    )
    assert [event.data for event in tail.events] == ["second\n"]


def test_request_rejects_empty_argv():
    with pytest.raises(ValueError, match="argv"):
        ProcessJobRequest(JobIdentity("j", "process", "execute", "i"), ())


@pytest.mark.parametrize("deadline", [0, -1, True, 86_401])
def test_request_rejects_invalid_deadline(deadline):
    with pytest.raises(ValueError, match="deadline_seconds"):
        ProcessJobRequest(
            JobIdentity("j", "process", "execute", "i"),
            ("python", "-c", "pass"),
            deadline_seconds=deadline,
        )


def test_worker_enforces_deadline_without_controller_polling(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "deadline-jobs.db")
    cleanup = ProcessTreeSupervisor(platform_name=os.name, timeout_seconds=5)
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=cleanup,
        platform_name=os.name,
    )
    job_id = "job-hard-deadline"
    started = provider.start(ProcessJobRequest(
        JobIdentity(job_id, "process", "execute", "idem-hard-deadline"),
        (sys.executable, "-c", "import time; time.sleep(30)"),
        cwd=tmp_path,
        deadline_seconds=1,
        max_descendants=4,
    ))
    process = provider._processes[job_id]

    deadline = time.monotonic() + 8
    while (
        (
            registry.poll(job_id).status is not JobStatus.CANCELLED
            or process.poll() is None
            or job_id in provider._processes
        )
        and time.monotonic() < deadline
    ):
        time.sleep(.05)

    assert registry.poll(job_id).status is JobStatus.CANCELLED
    assert "deadline" in registry.poll(job_id).error
    assert process.poll() is not None
    assert job_id not in provider._processes


def test_provider_rehydrates_persisted_deadline_after_owner_restart(tmp_path):
    database = tmp_path / "deadline-restart.db"
    registry = SQLiteDurableJobRegistry(database)
    cleanup = ProcessTreeSupervisor(platform_name=os.name, timeout_seconds=5)
    first = SubprocessJobProvider(
        registry,
        process_cleanup=cleanup,
        platform_name=os.name,
    )
    job_id = "job-restarted-deadline"
    first.start(ProcessJobRequest(
        JobIdentity(job_id, "process", "execute", "idem-restarted-deadline"),
        (sys.executable, "-c", "import time; time.sleep(30)"),
        cwd=tmp_path,
        deadline_seconds=2,
        max_descendants=4,
    ))
    process = first._processes[job_id]
    first._discard_deadline(job_id)

    reopened = SQLiteDurableJobRegistry(database)
    second = SubprocessJobProvider(
        reopened,
        process_cleanup=cleanup,
        platform_name=os.name,
    )
    deadline = time.monotonic() + 9
    while (
        (
            reopened.poll(job_id).status is not JobStatus.CANCELLED
            or process.poll() is None
            or job_id in second._deadline_timers
        )
        and time.monotonic() < deadline
    ):
        time.sleep(.05)

    assert reopened.poll(job_id).status is JobStatus.CANCELLED
    assert "deadline" in reopened.poll(job_id).error
    assert process.poll() is not None
    assert job_id not in second._deadline_timers


def test_deadline_reaps_an_already_completed_process_instead_of_cancelling(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "completed-before-deadline.db")
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=ProcessTreeSupervisor(platform_name=os.name),
        platform_name=os.name,
    )
    job_id = "job-completed-before-deadline"
    provider.start(ProcessJobRequest(
        JobIdentity(job_id, "process", "execute", "idem-completed-before-deadline"),
        (sys.executable, "-c", "pass"),
        cwd=tmp_path,
        deadline_seconds=1,
    ))

    deadline = time.monotonic() + 5
    while not registry.poll(job_id).is_terminal and time.monotonic() < deadline:
        time.sleep(.05)

    assert registry.poll(job_id).status is JobStatus.SUCCEEDED


def test_restarted_deadline_never_signals_a_reused_process_identity(tmp_path):
    database = tmp_path / "pid-reuse.db"
    registry = SQLiteDurableJobRegistry(database)
    cleanup = _Cleanup(complete=True)
    process = _Process()
    first = SubprocessJobProvider(
        registry,
        process_cleanup=cleanup,
        launcher=lambda _argv, **_kwargs: process,
        platform_name="posix",
        process_identity_resolver=lambda _pid: "birth-A",
        memory_limiter=_MemoryLimiter(),
    )
    job_id = "job-pid-reused"
    first.start(ProcessJobRequest(
        JobIdentity(job_id, "process", "execute", "idem-pid-reused"),
        ("python", "-c", "pass"),
        deadline_seconds=1,
    ))
    first._discard_deadline(job_id)

    reopened = SQLiteDurableJobRegistry(database)
    second = SubprocessJobProvider(
        reopened,
        process_cleanup=cleanup,
        launcher=lambda _argv, **_kwargs: process,
        platform_name="posix",
        process_probe=lambda _pid, _expected: (PROCESS_ALIVE, "birth-B"),
        memory_limiter=_MemoryLimiter(),
    )
    deadline = time.monotonic() + 5
    while reopened.poll(job_id).status is not JobStatus.INTERRUPTED and time.monotonic() < deadline:
        time.sleep(.05)

    assert reopened.poll(job_id).status is JobStatus.INTERRUPTED
    assert "identity" in reopened.poll(job_id).error
    assert cleanup.requests == []
    assert job_id not in second._deadline_timers


def test_kind_scoped_recovery_is_not_capped_by_older_global_jobs():
    registry = DurableJobRegistry()
    for index in range(1_100):
        registry.start(JobIdentity(
            f"filler-{index}", "unrelated", f"op-{index}", f"idem-{index}"
        ))
    registry.start(JobIdentity(
        "compute-last", "compute-test", "controller-last", "idem-compute-last"
    ))
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=_Cleanup(complete=True),
        launcher=lambda _argv, **_kwargs: _Process(),
        platform_name="posix",
    )

    recovered = provider.recover(kind_prefix="compute-", limit=1024)
    assert [view.record.identity.job_id for view in recovered] == ["compute-last"]


# --- a child that exits before it can be fingerprinted ------------------------


def test_a_child_that_exits_before_fingerprinting_still_reports_its_result():
    """A deadline job whose process is gone by the time the identity probe
    reads it is not a failed launch: nothing is left for the deadline to
    enforce, and its exit code and output are still the job's result.
    Refusing it reported a finished job as "deadline jobs require a durable
    process identity", which is what a loaded host produced whenever the
    launcher thread lost the CPU between Popen and the probe."""
    if os.name != "posix":
        pytest.skip("real process identities are probed through /proc here")

    def launch_and_reap(argv, **kwargs):
        process = subprocess.Popen(argv, **kwargs)
        process.wait()  # exited and reaped before the provider can look at it
        return process

    registry = DurableJobRegistry()
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=_Cleanup(complete=True),
        launcher=launch_and_reap,
        platform_name=os.name,
        memory_limiter=_MemoryLimiter(),
    )
    request = ProcessJobRequest(
        JobIdentity("job-fast-exit", "process", "execute", "idem-fast-exit"),
        (sys.executable, "-c", "print('finished before the probe')"),
        deadline_seconds=30,
    )

    started = provider.start(request)
    assert started.process_id > 0
    view = registry.view(request.identity.job_id)
    assert view.metadata["launch_state"] == "attached"
    assert view.metadata["process_instance_identity"] == ""
    assert view.metadata["process_exited_before_fingerprint"] == "1"

    finished = provider.wait(request.identity.job_id, timeout=5)
    assert finished.timed_out is False
    assert finished.exit_code == 0
    assert finished.record.status is JobStatus.SUCCEEDED
    assert finished.record.result == {"exit_code": 0}


def test_a_live_process_without_a_durable_identity_is_still_refused():
    """The refusal stays for the case it exists for: a process that is alive
    but cannot be fingerprinted, which a later deadline could only kill by
    pid and so might kill whatever recycled that pid."""
    launched = []

    def launch_sleeping(argv, **kwargs):
        del argv
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], **kwargs,
        )
        launched.append(process)
        return process

    provider = SubprocessJobProvider(
        DurableJobRegistry(),
        process_cleanup=_Cleanup(complete=True),
        launcher=launch_sleeping,
        platform_name=os.name,
        memory_limiter=_MemoryLimiter(),
        process_identity_resolver=lambda _pid: None,
    )
    request = ProcessJobRequest(
        JobIdentity("job-no-identity", "process", "execute", "idem-no-identity"),
        (sys.executable, "-c", "pass"),
        deadline_seconds=30,
    )
    try:
        with pytest.raises(RuntimeError, match="durable process identity"):
            provider.start(request)
    finally:
        for process in launched:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.parametrize("durable", [False, True])
def test_explicit_cleanup_proof_survives_only_actual_scope_release(tmp_path, durable):
    from dataclasses import replace

    registry = (
        SQLiteDurableJobRegistry(tmp_path / "proof.db")
        if durable
        else DurableJobRegistry()
    )
    token = _ScopedToken(ProcessContainmentResult(True))
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=_Cleanup(complete=True),
        launcher=lambda *a, **k: _Process(),
        platform_name="posix",
        memory_limiter=_ScopedLimiter(token),
        process_identity_resolver=lambda pid: "proof-instance",
    )
    request = replace(_request("proof"), require_job_scope=True)
    provider.start(request)
    assert provider.cleanup_proof("proof") is None
    provider.wait("proof")
    proof = provider.cleanup_proof("proof")
    assert token.closed and proof["process_exited"] and proof["containment_empty"]
    assert proof["resources_released"] and proof["exit_code"] == 0
    assert proof["process_identity"] == "proof-instance"
    if durable:
        assert (
            SQLiteDurableJobRegistry(tmp_path / "proof.db").process_cleanup_proof(
                "proof"
            )
            == proof
        )


def test_cleanup_proof_write_failure_cannot_be_inferred_from_terminal(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    registry = SQLiteDurableJobRegistry(tmp_path / "proof.db")
    token = _ScopedToken(ProcessContainmentResult(True))
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=_Cleanup(complete=True),
        launcher=lambda *a, **k: _Process(),
        platform_name="posix",
        memory_limiter=_ScopedLimiter(token),
        process_identity_resolver=lambda pid: "proof-instance",
    )
    provider.start(replace(_request("proof"), require_job_scope=True))

    def fail(*args):
        raise OSError("injected durable proof failure")

    monkeypatch.setattr(registry, "_record_process_cleanup", fail)
    with pytest.raises(OSError):
        provider.wait("proof")
    assert registry.poll("proof").is_terminal
    assert provider.cleanup_proof("proof") is None


@pytest.mark.skipif(
    os.name != "nt", reason="real Windows Job Object descendant accounting"
)
def test_real_windows_descendant_is_removed_before_cleanup_certificate(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    provider = SubprocessJobProvider(
        registry, process_cleanup=ProcessTreeSupervisor(), max_concurrent_processes=1
    )
    script = (
        'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],'
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid,flush=True)"
    )
    request = ProcessJobRequest(
        JobIdentity("descendant-proof", "test", "test", "once"),
        (sys.executable, "-c", script),
        cwd=tmp_path,
        require_job_scope=True,
        max_descendants=3,
        memory_limit_bytes=256 * 1024 * 1024,
        deadline_seconds=10,
    )
    provider.start(request)
    waited = provider.wait("descendant-proof", timeout=5)
    assert waited.record.status is JobStatus.CANCELLED
    proof = provider.cleanup_proof("descendant-proof")
    assert (
        proof["process_exited"]
        and proof["containment_empty"]
        and proof["resources_released"]
    )
    assert proof["status"] == "cancelled"
    assert proof["scope_identity"]["containment_identity"].startswith(
        "Local\\SonderProcess-"
    )
    from dataclasses import replace

    second = replace(
        request,
        identity=JobIdentity("after-cleanup", "test", "test2", "twice"),
        argv=(sys.executable, "-c", "pass"),
    )
    provider.start(second)
    assert (
        provider.wait("after-cleanup", timeout=5).record.status is JobStatus.SUCCEEDED
    )


@pytest.mark.parametrize("ending", ["cancel", "forced_wait"])
@pytest.mark.parametrize("failure_stage", ["close", "capacity"])
def test_terminal_cancelled_job_retries_failed_close_before_releasing_slot(
    monkeypatch, ending, failure_stage
):
    from dataclasses import replace

    registry = DurableJobRegistry()
    token = _ScopedToken(ProcessContainmentResult(True, forced=True))
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=_Cleanup(complete=True),
        launcher=lambda *a, **k: _Process(),
        platform_name="posix",
        memory_limiter=_ScopedLimiter(token),
        max_concurrent_processes=1,
        process_identity_resolver=lambda pid: "stable",
    )
    retries = []
    monkeypatch.setattr(
        provider, "_schedule_deadline", lambda job_id, delay: retries.append(job_id)
    )
    provider.start(replace(_request("close-retry"), require_job_scope=True))
    close = token.close
    failures = [True]

    def fail_once():
        if failures:
            failures.pop()
            raise OSError("native close unavailable")
        close()

    if failure_stage == "close":
        monkeypatch.setattr(token, "close", fail_once)
    else:
        monkeypatch.setattr(provider, "_release_capacity", lambda job_id: fail_once())
    with pytest.raises(OSError):
        if ending == "cancel":
            provider.cancel("close-retry")
        else:
            provider.wait("close-retry")
    assert "close-retry" in retries
    assert registry.poll("close-retry").is_terminal
    assert provider.cleanup_proof("close-retry") is None
    with pytest.raises(RuntimeError, match="capacity"):
        provider.start(_request("too-soon"))
    provider._expire_deadline_owned("close-retry")
    assert token.closed
    assert provider.cleanup_proof("close-retry")["resources_released"]
    provider.start(replace(_request("after-close"), require_job_scope=True))
    provider.wait("after-close")


@pytest.mark.parametrize("cancel", [False, True])
def test_prepared_scope_memory_limit_preserves_exact_cleanup_owner(cancel):
    from dataclasses import replace
    token = _ScopedToken(ProcessContainmentResult(True))
    class Limiter(_ScopedLimiter):
        def apply(self, process, memory_limit_bytes):
            pytest.fail("prepared scope must not be replaced by a per-process token")
    provider = SubprocessJobProvider(
        DurableJobRegistry(), process_cleanup=_Cleanup(complete=True),
        launcher=lambda *args, **kwargs: _Process(), platform_name="posix",
        memory_limiter=Limiter(token), process_identity_resolver=lambda pid: "exact-scope-instance")
    request = replace(_request("scope-memory"), require_job_scope=True, memory_limit_bytes=512 * 1024 * 1024)
    try:
        provider.start(request)
        assert provider._memory_tokens["scope-memory"] is token
        if cancel:
            assert provider.cancel("scope-memory").cleanup_completed
        else:
            provider.wait("scope-memory")
        proof = provider.cleanup_proof("scope-memory")
        assert token.closed and proof["containment_empty"] and proof["resources_released"]
    finally:
        if "scope-memory" in provider._processes:
            provider.cancel("scope-memory", reason="test fixture cleanup")


@pytest.mark.parametrize("key", ["SECRET_TOKEN", "LD_PRELOAD", "DBUS_SESSION_BUS_ADDRESS"])
def test_unsupported_isolated_systemd_environment_refuses_before_launch(key):
    from dataclasses import replace
    from types import SimpleNamespace
    from sonder_runtime.adapters.extensions.memory_limits import NativeExtensionMemoryLimiter, ExtensionMemoryLimitUnsupported
    commands = []
    launches = []
    limiter = NativeExtensionMemoryLimiter(
        os_module=SimpleNamespace(name="posix", environ={}, geteuid=lambda: 1000),
        platform_name="posix", which=lambda name: f"/usr/bin/{name}",
        command_runner=lambda *args, **kwargs: commands.append(args))
    provider = SubprocessJobProvider(DurableJobRegistry(), process_cleanup=_Cleanup(complete=True),
        launcher=lambda *args, **kwargs: launches.append(args), platform_name="posix", memory_limiter=limiter)
    request = replace(_request("unsupported-env"), require_job_scope=True, inherit_environment=False,
        environment=((key, "secret-value"),))
    with pytest.raises(ExtensionMemoryLimitUnsupported, match="unsupported keys"):
        provider.start(request)
    assert not launches and not commands and not provider._processes and not provider._memory_tokens
