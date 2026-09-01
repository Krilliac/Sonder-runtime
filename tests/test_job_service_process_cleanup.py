"""Application-seam tests for bounded job process-tree cleanup."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from sonder_runtime.application.capabilities.jobs import JobRegistryService
from sonder_runtime.application.jobs.durable_registry import ProcessTreeCleanupReceipt
from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus
from sonder_runtime.domain.common.errors import InvalidInput


def _identity(job_id: str = "job-1") -> JobIdentity:
    return JobIdentity(job_id, "shell", "op-1", f"idem-{job_id}")


class _Registry:
    def __init__(self, *, process_id: int | None = 41, process_group_id: int | None = 40) -> None:
        self.record = JobRecord(_identity(), status=JobStatus.RUNNING)
        self.metadata = SimpleNamespace(process_id=process_id, process_group_id=process_group_id)

    def cancel(
        self,
        job_id: str,
        *,
        reason: str = "cancelled",
        max_descendants: int = 256,
    ) -> tuple[JobRecord, ...]:
        if job_id != self.record.identity.job_id:
            raise KeyError(job_id)
        self.record = JobRecord(
            self.record.identity,
            JobStatus.CANCELLED,
            self.record.revision + 1,
            error=reason,
        )
        return (self.record,)

    def request_cancellation(
        self,
        job_id: str,
        *,
        reason: str = "cancelled",
        max_descendants: int = 256,
    ) -> tuple[JobRecord, ...]:
        if job_id != self.record.identity.job_id:
            raise KeyError(job_id)
        self.record = JobRecord(
            self.record.identity,
            JobStatus.CANCELLATION_REQUESTED,
            self.record.revision + 1,
            error=reason,
        )
        return (self.record,)

    def view(self, job_id: str):
        if job_id != self.record.identity.job_id:
            raise KeyError(job_id)
        return self.metadata


class _Supervisor:
    def __init__(self, receipt: ProcessTreeCleanupReceipt) -> None:
        self.receipt = receipt
        self.requests = []

    def cleanup(self, request):
        self.requests.append(request)
        return self.receipt


def test_cancel_with_cleanup_is_bounded_and_reports_complete_receipt():
    supervisor = _Supervisor(ProcessTreeCleanupReceipt("job-1", True, 2, 2, True, "clean"))
    service = JobRegistryService(_Registry(), process_cleanup=supervisor)

    result = service.cancel_with_cleanup("job-1", "operator requested", max_descendants=7)

    assert result.cleanup_completed is True
    assert result.records[0].status is JobStatus.CANCELLED
    assert result.records[0].is_terminal is True
    assert result.cleanup_receipts[0].complete is True
    assert supervisor.requests[0].process_id == 41
    assert supervisor.requests[0].max_descendants == 7
    assert supervisor.requests[0].reason == "operator requested"


def test_cancel_with_cleanup_preserves_incomplete_receipt_truthfully():
    supervisor = _Supervisor(ProcessTreeCleanupReceipt("job-1", True, 2, 1, False, "child remains"))
    service = JobRegistryService(_Registry(), process_cleanup=supervisor)

    result = service.cancel_with_cleanup("job-1")

    assert result.records[0].status is JobStatus.CANCELLATION_REQUESTED
    assert result.cleanup_completed is False
    assert result.cleanup_receipts[0].complete is False
    assert result.detail == "child remains"


def test_cancel_with_cleanup_requires_proof_when_supervisor_or_metadata_is_missing():
    no_supervisor = JobRegistryService(_Registry())
    result = no_supervisor.cancel_with_cleanup("job-1")
    assert result.cleanup_completed is False
    assert result.records[0].status is JobStatus.CANCELLATION_REQUESTED
    assert result.cleanup_receipts == ()
    assert "not configured" in result.detail

    no_process = JobRegistryService(_Registry(process_id=None), process_cleanup=_Supervisor(
        ProcessTreeCleanupReceipt("job-1", True, 0, 0, True, "not required")
    ))
    result = no_process.cancel_with_cleanup("job-1")
    assert result.cleanup_completed is False
    assert result.records[0].status is JobStatus.CANCELLATION_REQUESTED
    assert result.cleanup_receipts == ()
    assert "process id" in result.detail


def test_cancel_with_cleanup_rejects_unbounded_request():
    service = JobRegistryService(_Registry(), process_cleanup=_Supervisor(
        ProcessTreeCleanupReceipt("job-1", True, 0, 0, True)
    ))
    with pytest.raises(InvalidInput):
        service.cancel_with_cleanup("job-1", max_descendants=0)
