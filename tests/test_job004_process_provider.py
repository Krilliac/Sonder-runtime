from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
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
