"""A crashed compute submit is settled only by its exact durable process attach."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from sonder_runtime.adapters.execution.compute_effect_verifier import (
    DurableComputeSubmitVerifier,
    DurableLocalProcessStartVerifier,
)
from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
    SQLiteEffectJournal,
)
from sonder_runtime.adapters.persistence.sqlite.job_registry import (
    SQLiteDurableJobRegistry,
)
from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor
from sonder_runtime.application.compute_fabric.jobs import (
    ComputeJobWorker,
    JobCatalogEntry,
    RemoteJobEnvelope,
)
from sonder_runtime.application.execution.effect_journal import (
    EffectJournalError,
    EffectState,
)
from sonder_runtime.application.execution.worker_bindings import (
    AuthenticatedWorkerBinding,
)
from sonder_runtime.application.execution.worker_bindings import (
    _digest as effect_request_digest,
)
from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus
from sonder_runtime.domain.compute_fabric import WorkloadKind
from tests.test_compute_job_worker import _TestScopeLimiter

CRASH_EXIT = 86
RUN_ID = "runtime:compute-jobs"
WORKER_ID = "worker-1"
IDEMPOTENCY = "idem-1"


def _job_id() -> str:
    return "cf-" + hashlib.sha256(f"{WORKER_ID}\x00{IDEMPOTENCY}".encode()).hexdigest()[:24]


def _envelope() -> RemoteJobEnvelope:
    return RemoteJobEnvelope.create(
        controller_job_id="controller-job", idempotency_key=IDEMPOTENCY,
        workload=WorkloadKind.TEST, catalog_entry_id="once", workspace_mapping="project",
        idempotent=True, deadline_seconds=15,
    )


def _entry(root: Path) -> JobCatalogEntry:
    marker = root / "external-effect.log"
    code = (
        "import os,sys,time; time.sleep(.2); "
        "fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600); "
        "os.write(fd,b'x'); os.fsync(fd); os.close(fd)"
    )
    return JobCatalogEntry(
        entry_id="once", workload=WorkloadKind.TEST, program=sys.executable,
        fixed_args=("-c", code, str(marker)),
        workspace_mappings=frozenset({"project"}),
    )


def _journal(root: Path) -> SQLiteEffectJournal:
    return SQLiteEffectJournal(
        root / "effects.db",
        reconciliation_verifiers={
            "process-start": DurableLocalProcessStartVerifier(
                lambda: SQLiteDurableJobRegistry(root / "jobs.db")
            ),
            "compute-submit": DurableComputeSubmitVerifier(
                lambda: SQLiteDurableJobRegistry(root / "jobs.db")
            ),
        },
    )


def _binding(journal, epoch: int) -> AuthenticatedWorkerBinding:
    return AuthenticatedWorkerBinding(journal, RUN_ID, f"compute:{WORKER_ID}", epoch, "compute-jobs")


def _process_binding(journal, epoch: int) -> AuthenticatedWorkerBinding:
    return AuthenticatedWorkerBinding(
        journal, "runtime:process-jobs", f"process:{WORKER_ID}", epoch, "process-jobs",
    )


def _seed(root: Path, *, attach: bool = True, terminal: bool = True,
          terminal_status: JobStatus = JobStatus.SUCCEEDED,
          metadata_changes: dict | None = None):
    """Durable registry records, never caller-provided status text."""
    envelope = _envelope()
    metadata = {
        "compute_worker_id": WORKER_ID,
        "compute_controller_job_id": envelope.controller_job_id,
        "compute_request_sha256": envelope.request_sha256,
        "compute_effect_request_digest": effect_request_digest(envelope),
        "process_request_digest": "b" * 64,
        "require_job_scope": "1",
        "launch_state": "reserved",
    }
    metadata.update(metadata_changes or {})
    registry = SQLiteDurableJobRegistry(root / "jobs.db")
    registry.start(JobIdentity(
        _job_id(), "compute-test", envelope.controller_job_id, IDEMPOTENCY,
    ), metadata=metadata)
    if attach:
        registry.attach_process(
            _job_id(), process_id=12345,
            metadata={"launch_state": (
                metadata_changes["launch_state"]
                if metadata_changes is not None and "launch_state" in metadata_changes
                else "attached"
            )},
        )
    if terminal:
        registry.transition(_job_id(), terminal_status)
    journal = _journal(root)
    first = _binding(journal, 1)
    assert first.recover_before_restart().action == "resume"
    intent = first.binding().begin_request(
        operation_id=f"compute-submit:{WORKER_ID}:{IDEMPOTENCY}",
        idempotency_key=IDEMPOTENCY, request_digest=effect_request_digest(envelope),
        reconciliation="idempotent",
    )
    return journal, registry, intent


@pytest.mark.parametrize("invalid", [
    "no_attach", "still_running", "missing_digest", "wrong_digest",
    "wrong_worker", "unscoped", "wrong_controller", "unmatched_launch",
    "legacy_request", "wrong_kind", "wrong_idempotency",
])
def test_compute_verifier_fences_unproven_or_mismatched_launch(tmp_path, invalid):
    changes = {
        "missing_digest": {"compute_effect_request_digest": None},
        "wrong_digest": {"compute_effect_request_digest": "c" * 64},
        "wrong_worker": {"compute_worker_id": "other"},
        "unscoped": {"require_job_scope": "0"},
        "wrong_controller": {"compute_controller_job_id": "other"},
        "unmatched_launch": {"launch_state": "reserved"},
        "legacy_request": {"process_request_digest": None},
    }.get(invalid)
    journal, registry, intent = _seed(
        tmp_path, attach=invalid != "no_attach", terminal=invalid != "still_running",
        metadata_changes=changes,
    )
    if invalid in {"wrong_kind", "wrong_idempotency"}:
        with sqlite3.connect(tmp_path / "jobs.db") as connection:
            field, value = ("kind", "process") if invalid == "wrong_kind" else (
                "idempotency_key", "other"
            )
            connection.execute(f"UPDATE durable_job SET {field}=? WHERE job_id=?", (value, _job_id()))
    assert DurableComputeSubmitVerifier(lambda: registry).verify(intent) is None
    with pytest.raises(EffectJournalError, match="reconciliation"):
        _binding(journal, 2).recover_before_restart()
    assert journal.get(intent.intent_id).state is EffectState.UNCERTAIN


@pytest.mark.parametrize("job_status", [
    JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED,
])
def test_attached_terminal_job_proves_submit_even_if_the_job_failed(tmp_path, job_status):
    journal, registry, intent = _seed(tmp_path, terminal_status=job_status)
    proof = DurableComputeSubmitVerifier(lambda: registry).verify(intent)
    assert proof is not None
    assert proof.state is EffectState.COMPLETED
    assert proof.receipt_key == _job_id()
    with pytest.raises(EffectJournalError, match="reconciliation"):
        _binding(journal, 2).recover_before_restart()
    with pytest.raises(EffectJournalError, match="stale reconciliation owner epoch"):
        journal.reconcile(intent.intent_id, owner_epoch=1)
    assert journal.reconcile(intent.intent_id, owner_epoch=2).state is EffectState.COMPLETED
    assert _binding(journal, 2).recover_before_restart().action == "resume"


@pytest.mark.parametrize("invalid", [
    "no_attach", "still_running", "wrong_process_digest", "wrong_worker",
    "unscoped", "wrong_kind", "wrong_idempotency", "wrong_controller",
])
def test_compute_process_start_verifier_fences_unproven_launch(tmp_path, invalid):
    changes = {
        "wrong_process_digest": {"process_request_digest": "c" * 64},
        "wrong_worker": {"compute_worker_id": "other"},
        "unscoped": {"require_job_scope": "0"},
        "wrong_controller": {"compute_controller_job_id": "other"},
    }.get(invalid)
    journal, registry, _ = _seed(
        tmp_path, attach=invalid != "no_attach", terminal=invalid != "still_running",
        metadata_changes=changes,
    )
    if invalid in {"wrong_kind", "wrong_idempotency"}:
        with sqlite3.connect(tmp_path / "jobs.db") as connection:
            field, value = ("kind", "process") if invalid == "wrong_kind" else (
                "idempotency_key", "other"
            )
            connection.execute(f"UPDATE durable_job SET {field}=? WHERE job_id=?", (value, _job_id()))
    process = _process_binding(journal, 1)
    assert process.recover_before_restart().action == "resume"
    intent = process.binding().begin_request(
        operation_id=f"process-start:{_job_id()}", idempotency_key=IDEMPOTENCY,
        request_digest="b" * 64, reconciliation="idempotent",
    )
    assert DurableLocalProcessStartVerifier(lambda: registry).verify(intent) is None
    with pytest.raises(EffectJournalError, match="reconciliation"):
        _process_binding(journal, 2).recover_before_restart()
    assert journal.get(intent.intent_id).state is EffectState.UNCERTAIN


@pytest.mark.parametrize("job_status", [
    JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED,
])
def test_compute_process_start_proof_is_launch_not_job_result(tmp_path, job_status):
    journal, registry, _ = _seed(tmp_path, terminal_status=job_status)
    process = _process_binding(journal, 1)
    assert process.recover_before_restart().action == "resume"
    intent = process.binding().begin_request(
        operation_id=f"process-start:{_job_id()}", idempotency_key=IDEMPOTENCY,
        request_digest="b" * 64, reconciliation="idempotent",
    )
    proof = DurableLocalProcessStartVerifier(lambda: registry).verify(intent)
    assert proof is not None
    assert proof.receipt_key == f"{_job_id()}:12345"
    assert proof.state is EffectState.COMPLETED
    with pytest.raises(EffectJournalError, match="reconciliation"):
        _process_binding(journal, 2).recover_before_restart()
    with pytest.raises(EffectJournalError, match="stale reconciliation owner epoch"):
        journal.reconcile(intent.intent_id, owner_epoch=1)
    assert journal.reconcile(intent.intent_id, owner_epoch=2).state is EffectState.COMPLETED
    assert _process_binding(journal, 2).recover_before_restart().action == "resume"


def test_generic_process_start_proof_remains_available(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    registry.start(
        JobIdentity("ordinary-job", "process", "ordinary-operation", "ordinary-idem"),
        metadata={"process_request_digest": "a" * 64, "launch_state": "reserved"},
    )
    registry.attach_process(
        "ordinary-job", process_id=12345, metadata={"launch_state": "attached"},
    )
    registry.transition("ordinary-job", JobStatus.SUCCEEDED)
    journal = _journal(tmp_path)
    owner = _process_binding(journal, 1)
    assert owner.recover_before_restart().action == "resume"
    intent = owner.binding().begin_request(
        operation_id="process-start:ordinary-job", idempotency_key="ordinary-idem",
        request_digest="a" * 64, reconciliation="idempotent",
    )
    proof = DurableLocalProcessStartVerifier(lambda: registry).verify(intent)
    assert proof is not None
    assert proof.verifier_id == "durable-process-job-registry-v1"
    assert proof.receipt_key == "ordinary-job:12345"


def _crash_after_durable_attach(root: Path, before_process_receipt: bool) -> None:
    journal = _journal(root)
    registry = SQLiteDurableJobRegistry(root / "jobs.db")
    entry = _entry(root)
    process = SubprocessJobProvider(
        registry, process_cleanup=ProcessTreeSupervisor(platform_name=os.name),
        memory_limiter=_TestScopeLimiter(), platform_name=os.name,
        process_identity_resolver=lambda _pid: "stable",
        effect_binding=AuthenticatedWorkerBinding(
            journal, "runtime:process-jobs", "process:worker-1", 1, "process-jobs",
        ),
    )
    original = process._start_unjournaled if before_process_receipt else process.start

    def after_attach(request):
        started = original(request)
        result = process.wait(started.record.identity.job_id, timeout=10)
        assert result.record.is_terminal
        os._exit(CRASH_EXIT)  # Child dies before ComputeJobWorker gets a receipt.

    if before_process_receipt:
        process._start_unjournaled = after_attach
    else:
        process.start = after_attach
    ComputeJobWorker(
        worker_id=WORKER_ID, catalog={"once": entry},
        workspace_mappings={"project": root}, provider=process,
        effect_binding=_binding(journal, 1),
    ).submit(_envelope())
    os._exit(2)  # A missing crash hook cannot pass the test.


@pytest.mark.skipif(os.name != "posix", reason="real POSIX process crash probe")
@pytest.mark.parametrize("before_process_receipt", [False, True])
def test_real_compute_process_is_reconciled_without_second_launch(tmp_path, before_process_receipt):
    repo = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(repo), os.environ.get("PYTHONPATH", "")))}
    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path),
         "inner" if before_process_receipt else "outer"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert child.returncode == CRASH_EXIT, (child.returncode, child.stderr[-2000:])
    marker = tmp_path / "external-effect.log"
    assert marker.read_text() == "x"
    journal = _journal(tmp_path)
    intent = journal.get(f"{RUN_ID}:compute-submit:{WORKER_ID}:{IDEMPOTENCY}")
    assert intent is not None and intent.state is EffectState.INTENT
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    view = registry.view(_job_id())
    assert view.record.is_terminal and view.process_id > 0
    assert view.metadata["launch_state"] == "attached"
    assert view.metadata["compute_effect_request_digest"] == intent.request_digest

    process_intent = journal.get(f"runtime:process-jobs:process-start:{_job_id()}")
    assert process_intent is not None
    assert process_intent.state is (
        EffectState.INTENT if before_process_receipt else EffectState.COMPLETED
    )
    assert process_intent.request_digest == view.metadata["process_request_digest"]
    process_owner = _process_binding(journal, 2)
    if before_process_receipt:
        with pytest.raises(EffectJournalError, match="reconciliation"):
            process_owner.recover_before_restart()
        assert journal.get(process_intent.intent_id).state is EffectState.UNCERTAIN
        with pytest.raises(EffectJournalError, match="stale reconciliation owner epoch"):
            journal.reconcile(process_intent.intent_id, owner_epoch=1)
        assert journal.reconcile(process_intent.intent_id, owner_epoch=2).state is EffectState.COMPLETED
    assert process_owner.recover_before_restart().action == "resume"
    assert journal.effects_since("runtime:process-jobs", 0).records[0].state is EffectState.COMPLETED

    second = _binding(journal, 2)
    with pytest.raises(EffectJournalError, match="reconciliation"):
        second.recover_before_restart()
    assert journal.get(intent.intent_id).state is EffectState.UNCERTAIN
    with pytest.raises(EffectJournalError):
        second.binding().begin_request(
            operation_id=intent.operation_id, idempotency_key=IDEMPOTENCY,
            request_digest=intent.request_digest, reconciliation="idempotent",
        )
    assert marker.read_text() == "x"

    settled = journal.reconcile(intent.intent_id, owner_epoch=2)
    assert settled.state is EffectState.COMPLETED
    assert settled.receipt_key == _job_id()
    assert second.recover_before_restart().action == "resume"
    assert journal.effects_since(RUN_ID, 0).records[0].state is EffectState.COMPLETED
    process = SubprocessJobProvider(
        registry, process_cleanup=ProcessTreeSupervisor(platform_name=os.name),
        launcher=lambda *_args, **_kwargs: pytest.fail("duplicate process launched"),
        memory_limiter=_TestScopeLimiter(), platform_name=os.name,
        effect_binding=process_owner,
    )
    worker = ComputeJobWorker(
        worker_id=WORKER_ID, catalog={"once": _entry(tmp_path)},
        workspace_mappings={"project": tmp_path}, provider=process,
        effect_binding=second,
    )
    assert worker.submit(_envelope()).remote_job_id == _job_id()
    assert marker.read_text() == "x"


if __name__ == "__main__":
    _crash_after_durable_attach(Path(sys.argv[1]), sys.argv[2] == "inner")
