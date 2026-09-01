from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sonder_runtime.application.compute_fabric.jobs import (
    ArgumentPolicy,
    ComputeJobWorker,
    JobCatalogEntry,
    RemoteJobEnvelope,
)
from sonder_runtime.application.execution.process_jobs import ProcessJobStart, ProcessJobWait
from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus
from sonder_runtime.domain.common.errors import Conflict, InvalidInput
from sonder_runtime.domain.compute_fabric import WorkloadKind


class CapturingProvider:
    def __init__(self) -> None:
        self.request = None
        self.cancelled = None

    def start(self, request):
        self.request = request
        return ProcessJobStart(
            JobRecord(request.identity, status=JobStatus.RUNNING),
            process_id=42,
            process_group_id=42,
        )

    def cancel(self, job_id, reason="cancelled"):
        self.cancelled = (job_id, reason)
        return {"quiescent": True}


def _entry() -> JobCatalogEntry:
    return JobCatalogEntry(
        entry_id="pytest",
        workload=WorkloadKind.TEST,
        program="python",
        fixed_args=("-m", "pytest"),
        argument_policy=ArgumentPolicy.RELATIVE_PATHS_AND_TEST_SELECTORS,
        environment_allowlist=frozenset({"PYTEST_ADDOPTS"}),
        workspace_mappings=frozenset({"sonder"}),
    )


def _envelope(**changes) -> RemoteJobEnvelope:
    values = dict(
        controller_job_id="controller-job",
        idempotency_key="idem-1",
        workload=WorkloadKind.TEST,
        catalog_entry_id="pytest",
        workspace_mapping="sonder",
        relative_cwd="tests",
        arguments=("test_api.py",),
        environment=(("PYTEST_ADDOPTS", "-q"),),
        deadline_seconds=60,
        idempotent=True,
    )
    values.update(changes)
    return RemoteJobEnvelope.create(**values)


def test_worker_resolves_catalog_program_and_workspace(tmp_path: Path) -> None:
    provider = CapturingProvider()
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=provider,
    )
    receipt = worker.submit(_envelope())
    assert provider.request.argv == ("python", "-m", "pytest", "test_api.py")
    assert provider.request.cwd == (tmp_path / "tests").resolve()
    assert provider.request.environment == (("PYTEST_ADDOPTS", "-q"),)
    assert receipt.worker_id == "worker-1"
    assert receipt.request_sha256 == _envelope().request_sha256
    assert receipt.state == "running"


def test_worker_rejects_unknown_catalog_workspace_and_environment(tmp_path: Path) -> None:
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=CapturingProvider(),
    )
    with pytest.raises(InvalidInput, match="catalog"):
        worker.submit(_envelope(catalog_entry_id="unknown"))
    with pytest.raises(InvalidInput, match="workspace"):
        worker.submit(_envelope(workspace_mapping="other"))
    with pytest.raises(InvalidInput, match="environment"):
        worker.submit(_envelope(environment=(("SECRET", "x"),)))


def test_worker_idempotency_returns_same_job_and_conflict_rejects(tmp_path: Path) -> None:
    provider = CapturingProvider()
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=provider,
    )
    first = worker.submit(_envelope())
    second = worker.submit(_envelope())
    assert second.remote_job_id == first.remote_job_id
    with pytest.raises(Conflict):
        worker.submit(_envelope(arguments=("different.py",)))


def test_worker_revalidates_digest_even_if_constructed_unsafely(tmp_path: Path) -> None:
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=CapturingProvider(),
    )
    envelope = _envelope()
    object.__setattr__(envelope, "request_sha256", "0" * 64)
    with pytest.raises(InvalidInput, match="digest"):
        worker.submit(envelope)


def test_worker_status_refreshes_terminal_state_and_cancel_reports_cleanup_truth(
    tmp_path: Path,
) -> None:
    class CompletedProvider(CapturingProvider):
        def wait(self, job_id, *, timeout=None):
            assert timeout == 0
            return ProcessJobWait(
                JobRecord(
                    JobIdentity(
                        job_id,
                        kind="compute-test",
                        operation_id="controller-job",
                        idempotency_key="idem-1",
                    ),
                    status=JobStatus.SUCCEEDED,
                ),
                exit_code=0,
            )

    provider = CompletedProvider()
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=provider,
    )
    started = worker.submit(_envelope())
    assert worker.status(started.remote_job_id).state == "succeeded"
    assert worker.cancel(started.remote_job_id, reason="done").state == "cancelled"
