"""RED tests: terminal job truth must survive cancel/status and reaping."""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from sonder_runtime.application.compute_fabric.jobs import ComputeJobWorker
from sonder_runtime.application.compute_fabric.spool_budget import reap_stale_directories
from sonder_runtime.application.execution.process_jobs import ProcessJobWait
from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus
from sonder_runtime.domain.common.errors import InvalidInput

from tests.test_compute_job_worker import CapturingProvider, _entry, _envelope


@pytest.fixture(autouse=True)
def _private_spools(tmp_path, monkeypatch):
    import tempfile

    private = tmp_path / "system-temp"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    snapshots = tmp_path / ".compute-artifact-snapshots"
    monkeypatch.setattr(
        ComputeJobWorker, "_artifact_stage_base", staticmethod(lambda: snapshots),
    )


def _terminal_provider(status: JobStatus):
    class TerminalProvider(CapturingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_calls = 0

        def cancel(self, job_id, reason="cancelled"):
            self.cancel_calls += 1
            return super().cancel(job_id, reason)

        def wait(self, job_id, *, timeout=None):
            return ProcessJobWait(
                JobRecord(
                    JobIdentity(job_id, "compute-test", "controller-job", "idem-1"),
                    status=status,
                ),
                exit_code=0 if status is JobStatus.SUCCEEDED else 1,
            )

    return TerminalProvider()


def _worker(tmp_path: Path, provider) -> ComputeJobWorker:
    (tmp_path / "tests").mkdir(exist_ok=True)
    return ComputeJobWorker(
        worker_id="worker-1", catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path}, provider=provider,
    )


@pytest.mark.parametrize(
    ("status", "state"),
    [
        (JobStatus.SUCCEEDED, "succeeded"),
        (JobStatus.FAILED, "failed"),
        (JobStatus.INTERRUPTED, "interrupted"),
    ],
)
def test_cancelling_a_terminal_job_keeps_its_receipt(tmp_path, status, state) -> None:
    provider = _terminal_provider(status)
    worker = _worker(tmp_path, provider)
    started = worker.submit(_envelope())
    assert worker.status(started.remote_job_id).state == state

    result = worker.cancel(started.remote_job_id, reason="late")

    assert result.state == state
    assert worker.status(started.remote_job_id).state == state
    assert provider.cancel_calls == 0


def test_artifact_error_does_not_hide_terminal_state_on_status(
    tmp_path, monkeypatch,
) -> None:
    worker = _worker(tmp_path, _terminal_provider(JobStatus.SUCCEEDED))
    started = worker.submit(_envelope())

    def boom(_receipt):
        raise InvalidInput("artifact collection failed")

    monkeypatch.setattr(worker, "_collect_artifacts", boom)

    for _ in range(2):  # every poll, not just the first
        assert worker.status(started.remote_job_id).state == "succeeded"


# Windows-only RED: on POSIX, unlinking a 0o400 file in a writable dir already works.
def test_reaper_removes_stale_stage_with_read_only_files(tmp_path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    stage = spool / "cf-stale"
    stage.mkdir()
    staged = stage / "input.bin"
    staged.write_bytes(b"abc")
    os.chmod(staged, 0o400)
    old = time.time() - 7 * 86400
    os.utime(stage, (old, old))
    try:
        removed = reap_stale_directories(spool, max_age_seconds=86400)
        assert removed == 1
        assert not stage.exists()
    finally:
        if staged.exists():
            os.chmod(staged, stat.S_IWRITE | stat.S_IREAD)


def test_artifact_error_crosses_the_wire_and_http_status(tmp_path, monkeypatch) -> None:
    from sonder_runtime.application.compute_fabric.wire import (
        job_receipt_from_wire, job_receipt_to_wire,
    )
    from sonder_runtime.interfaces.http.facades.compute_fabric import (
        dispatch_compute_job_status,
    )

    worker = _worker(tmp_path, _terminal_provider(JobStatus.SUCCEEDED))
    started = worker.submit(_envelope())

    def boom(_receipt):
        raise InvalidInput("artifact collection failed")

    monkeypatch.setattr(worker, "_collect_artifacts", boom)
    result = dispatch_compute_job_status(worker, started.remote_job_id)
    wire = result.body["job"]
    assert wire["state"] == "succeeded"
    assert wire["artifact_error"]
    assert job_receipt_from_wire(wire).artifact_error == wire["artifact_error"]
    assert job_receipt_from_wire(job_receipt_to_wire(job_receipt_from_wire(wire))) == (
        job_receipt_from_wire(wire)
    )


def test_receipt_rejects_unbounded_artifact_error(tmp_path) -> None:
    from dataclasses import replace

    worker = _worker(tmp_path, _terminal_provider(JobStatus.SUCCEEDED))
    receipt = worker.status(worker.submit(_envelope()).remote_job_id)
    with pytest.raises(ValueError):
        replace(receipt, artifact_error="x" * 513)
