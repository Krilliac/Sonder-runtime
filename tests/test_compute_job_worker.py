from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import sys

import pytest

from sonder_runtime.application.compute_fabric.jobs import (
    ArgumentPolicy,
    ComputeJobWorker,
    DigestBoundInput,
    JobCatalogEntry,
    RemoteJobEnvelope,
)
from sonder_runtime.application.execution.process_jobs import ProcessJobStart, ProcessJobWait
from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus
from sonder_runtime.domain.common.errors import Conflict, InvalidInput
from sonder_runtime.domain.compute_fabric import WorkloadKind
from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor


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
        program=sys.executable,
        fixed_args=("-m", "pytest"),
        argument_policy=ArgumentPolicy.RELATIVE_PATHS_AND_TEST_SELECTORS,
        environment_allowlist=frozenset({"PYTEST_ADDOPTS"}),
        workspace_mappings=frozenset({"sonder"}),
        memory_limit_bytes=512 * 1024 * 1024,
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
    assert provider.request.argv == (sys.executable, "-m", "pytest", "test_api.py")
    assert provider.request.cwd == (tmp_path / "tests").resolve()
    assert provider.request.environment == (("PYTEST_ADDOPTS", "-q"),)
    assert provider.request.deadline_seconds == 60
    assert provider.request.memory_limit_bytes == 512 * 1024 * 1024
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


def test_worker_rejects_unconfigured_options_and_controller_paths(tmp_path: Path) -> None:
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=CapturingProvider(),
    )
    with pytest.raises(InvalidInput, match="option"):
        worker.submit(_envelope(arguments=("--basetemp=C:/Windows/Temp",)))
    with pytest.raises(ValueError, match="workspace"):
        _envelope(arguments=("C:/Windows/Temp",))


def test_worker_accepts_only_explicit_typed_options(tmp_path: Path) -> None:
    entry = replace(
        _entry(),
        allowed_bounded_options=frozenset({"--color"}),
        allowed_relative_path_options=frozenset({"--basetemp"}),
    )
    provider = CapturingProvider()
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": entry},
        workspace_mappings={"sonder": tmp_path},
        provider=provider,
    )
    worker.submit(_envelope(arguments=("--color=yes", "--basetemp=.tmp", "test_api.py")))
    assert provider.request.argv[-3:] == (
        "--color=yes", "--basetemp=.tmp", "test_api.py",
    )


def test_worker_rejects_argument_symlink_that_escapes_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    link = tmp_path / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=CapturingProvider(),
    )
    with pytest.raises(InvalidInput, match="escape"):
        worker.submit(_envelope(relative_cwd=".", arguments=("escape/test_api.py",)))


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


def test_worker_verifies_digest_bound_inputs_before_launch(tmp_path: Path) -> None:
    import hashlib

    tests = tmp_path / "tests"
    tests.mkdir()
    payload = tests / "input.bin"
    payload.write_bytes(b"abc")
    provider = CapturingProvider()
    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=provider,
    )
    digest = hashlib.sha256(b"abc").hexdigest()
    worker.submit(_envelope(input_artifacts=(DigestBoundInput("input.bin", 3, digest),)))
    assert provider.request is not None

    with pytest.raises(InvalidInput, match="digest"):
        worker.submit(_envelope(
            idempotency_key="idem-bad-input",
            input_artifacts=(DigestBoundInput("input.bin", 3, "0" * 64),),
        ))


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


def test_worker_emits_verified_receipts_for_catalog_artifacts(tmp_path: Path) -> None:
    import hashlib

    tests = tmp_path / "tests"
    tests.mkdir()
    report = tests / "report.json"
    report.write_bytes(b'{"ok":true}')

    class CompletedProvider(CapturingProvider):
        def wait(self, job_id, *, timeout=None):
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

    worker = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": replace(_entry(), artifact_paths=("report.json",))},
        workspace_mappings={"sonder": tmp_path},
        provider=CompletedProvider(),
    )
    started = worker.submit(_envelope())
    completed = worker.status(started.remote_job_id)
    assert completed.artifacts[0].name == "report.json"
    assert completed.artifacts[0].size_bytes == len(b'{"ok":true}')
    assert completed.artifacts[0].sha256 == hashlib.sha256(b'{"ok":true}').hexdigest()
    payload = worker.read_artifact(started.remote_job_id, "report.json")
    assert payload.content == b'{"ok":true}'

    report.write_bytes(b'{"ok":false}')
    with pytest.raises(InvalidInput, match="changed"):
        worker.read_artifact(started.remote_job_id, "report.json")


def test_worker_rehydrates_digest_bound_receipt_after_restart(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    database = tmp_path / "compute-restart.db"
    cleanup = ProcessTreeSupervisor(platform_name=os.name, timeout_seconds=5)
    first_provider = SubprocessJobProvider(
        SQLiteDurableJobRegistry(database),
        process_cleanup=cleanup,
        platform_name=os.name,
    )
    entry = replace(_entry(), program=sys.executable)
    first = ComputeJobWorker(
        worker_id="worker-1",
        catalog={"pytest": entry},
        workspace_mappings={"sonder": tmp_path},
        provider=first_provider,
    )
    envelope = _envelope(arguments=("test_api.py",), deadline_seconds=30)
    started = first.submit(envelope)
    try:
        reopened_provider = SubprocessJobProvider(
            SQLiteDurableJobRegistry(database),
            process_cleanup=cleanup,
            platform_name=os.name,
        )
        reopened = ComputeJobWorker(
            worker_id="worker-1",
            catalog={"pytest": entry},
            workspace_mappings={"sonder": tmp_path},
            provider=reopened_provider,
        )

        recovered = reopened.by_idempotency(envelope.idempotency_key)
        assert recovered is not None
        assert recovered.remote_job_id == started.remote_job_id
        assert recovered.controller_job_id == envelope.controller_job_id
        assert recovered.request_sha256 == envelope.request_sha256
        assert reopened.status(started.remote_job_id).state in {"pending", "running"}
    finally:
        first_provider.cancel(started.remote_job_id, reason="test cleanup")
