from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor
from sonder_runtime.application.execution.process_jobs import ProcessJobRequest
from sonder_runtime.application.jobs.durable_registry import (
    DurableJobRegistry,
    ProcessTreeCleanupReceipt,
)
from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus


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


def test_request_rejects_empty_argv():
    with pytest.raises(ValueError, match="argv"):
        ProcessJobRequest(JobIdentity("j", "process", "execute", "i"), ())
