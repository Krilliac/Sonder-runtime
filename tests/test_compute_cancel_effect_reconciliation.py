"""A crashed compute cancel is settled only by exact durable registry evidence (#515).

The ``compute-cancel`` verifier may prove an interrupted cancel only when the
durable job registry shows, for the exact remote job this worker minted, a
terminal ``cancelled`` record with immutable cleanup evidence *and* a durable
binding of this attempt's idempotency key to the journaled request digest,
made while the record was not yet terminal.  Every other shape (legacy rows
without the binding, other digests, kinds, keys, workers, runs, scopes,
non-terminal or non-cancelled states, missing cleanup, a binding made only
after the job was already terminal, a swapped job) produces no proof, so
restart stays fenced.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from sonder_runtime.adapters.execution.compute_effect_verifier import (
    DurableComputeCancelVerifier,
)
from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
from sonder_runtime.adapters.extensions.memory_limits import ProcessContainmentResult
from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
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
    _digest as effect_request_digest,
)
from sonder_runtime.application.jobs.durable_registry import (
    CANCEL_REQUEST_BOUND_STATES,
    CANCEL_REQUEST_DIGESTS,
    DurableJobRegistry,
    MAX_CANCEL_REQUEST_BINDINGS,
)
from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus
from sonder_runtime.domain.compute_fabric import WorkloadKind

RUN_ID = "runtime:compute-jobs"
WORKER_ID = "worker-1"
SUBMIT_KEY = "idem-1"
OTHER_KEY = "idem-2"
REASON = "operator cancel"


def _job_id(key: str = SUBMIT_KEY, worker: str = WORKER_ID) -> str:
    return "cf-" + hashlib.sha256(f"{worker}\x00{key}".encode()).hexdigest()[:24]


def _cancel_identity(job_id: str, attempt: int = 1) -> tuple[str, str]:
    if attempt == 1:
        return f"compute-cancel:{WORKER_ID}:{job_id}", f"cancel:{job_id}"
    return (
        f"compute-cancel:{WORKER_ID}:attempt-{attempt}:{job_id}",
        f"cancel:{job_id}:attempt-{attempt}",
    )


def _request_digest(job_id: str, reason: str = REASON) -> str:
    # The exact request ComputeJobWorker.cancel journals.
    return effect_request_digest({"remote_job_id": job_id, "reason": reason})


def _publish_cleanup(registry, job_id: str) -> dict:
    """Mirror ``SubprocessJobProvider._publish_cleanup`` for a terminal record."""
    view = registry.view(job_id)
    proof = dict(
        job_id=job_id,
        job_revision=view.record.revision,
        parent_session_id=view.record.identity.parent_session_id,
        principal_id="",
        process_id=view.process_id,
        process_identity="stable",
        scope_identity={},
        process_exited=True,
        containment_empty=True,
        resources_released=True,
        status=view.record.status.value,
        exit_code=-9,
    )
    proof["digest"] = hashlib.sha256(
        json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    registry._record_process_cleanup(job_id, proof)
    return proof


def _start_job(registry, key: str = SUBMIT_KEY, **changes) -> str:
    job_id = _job_id(key)
    metadata = {
        "compute_worker_id": WORKER_ID,
        "compute_controller_job_id": "controller-job",
        "compute_request_sha256": "c" * 64,
        "compute_effect_request_digest": "d" * 64,
        "process_request_digest": "e" * 64,
        "require_job_scope": "1",
        "launch_state": "reserved",
    }
    metadata.update(changes)
    registry.start(JobIdentity(job_id, "compute-test", "controller-job", key), metadata=metadata)
    registry.attach_process(job_id, process_id=4242, metadata={"launch_state": "attached"})
    return job_id


def _cancel_durably(registry, job_id: str, *, clean: bool = True) -> None:
    registry.request_cancellation(job_id, reason=REASON)
    if clean:
        registry.cancel(job_id, reason=REASON)
        _publish_cleanup(registry, job_id)


def _journal(root: Path, registry_getter=None) -> SQLiteEffectJournal:
    getter = registry_getter or (lambda: SQLiteDurableJobRegistry(root / "jobs.db"))
    return SQLiteEffectJournal(
        root / "effects.db",
        reconciliation_verifiers={"compute-cancel": DurableComputeCancelVerifier(getter)},
    )


def _binding(journal, epoch: int, *, auto: bool = False) -> AuthenticatedWorkerBinding:
    return AuthenticatedWorkerBinding(
        journal, RUN_ID, f"compute:{WORKER_ID}", epoch, "compute-jobs",
        auto_reconcile=auto,
    )


def _admit(journal, job_id: str, *, attempt: int = 1, digest: str | None = None,
           idempotency_key: str | None = None, reconciliation: str = "idempotent",
           binding_factory=None):
    operation_id, key = _cancel_identity(job_id, attempt)
    first = (binding_factory or _binding)(journal, 1)
    assert first.recover_before_restart().action == "resume"
    return first.binding().begin_request(
        operation_id=operation_id,
        idempotency_key=idempotency_key or key,
        request_digest=digest or _request_digest(job_id),
        reconciliation=reconciliation,
    )


def _seed(root: Path, *, attempt: int = 1, bind: bool = True, clean: bool = True):
    registry = SQLiteDurableJobRegistry(root / "jobs.db")
    job_id = _start_job(registry)
    if bind:
        registry.bind_cancel_request(
            job_id, idempotency_key=_cancel_identity(job_id, attempt)[1],
            request_digest=_request_digest(job_id),
        )
    _cancel_durably(registry, job_id, clean=clean)
    journal = _journal(root)
    intent = _admit(journal, job_id, attempt=attempt)
    return journal, registry, job_id, intent


def _assert_fenced(journal, intent, binding_factory=None) -> None:
    with pytest.raises(EffectJournalError, match="reconciliation"):
        (binding_factory or _binding)(journal, 2).recover_before_restart()
    assert journal.get(intent.intent_id).state is EffectState.UNCERTAIN
    with pytest.raises(EffectJournalError, match="no trusted proof"):
        journal.reconcile(intent.intent_id, owner_epoch=2)
    assert journal.get(intent.intent_id).state is EffectState.UNCERTAIN


@pytest.mark.parametrize("attempt", [1, 2, 32])
def test_cleaned_cancelled_record_with_exact_binding_proves_the_cancel(tmp_path, attempt):
    journal, registry, job_id, intent = _seed(tmp_path, attempt=attempt)
    proof = DurableComputeCancelVerifier(lambda: registry).verify(intent)
    assert proof is not None
    assert proof.state is EffectState.COMPLETED
    assert proof.receipt_key == f"{job_id}:cancelled"
    assert proof.verifier_id == "durable-compute-cancel-v1"
    assert proof.external_reference == f"job-registry:{job_id}:{registry.poll(job_id).revision}"
    # The proof is deterministic: the same durable state gives the same digest.
    assert DurableComputeCancelVerifier(lambda: registry).verify(intent) == proof

    with pytest.raises(EffectJournalError, match="reconciliation"):
        _binding(journal, 2).recover_before_restart()
    with pytest.raises(EffectJournalError, match="stale reconciliation owner epoch"):
        journal.reconcile(intent.intent_id, owner_epoch=1)
    settled = journal.reconcile(intent.intent_id, owner_epoch=2)
    assert settled.state is EffectState.COMPLETED
    assert settled.receipt_key == f"{job_id}:cancelled"
    assert settled.detail.startswith("verified:durable-compute-cancel-v1:job-registry:")
    assert _binding(journal, 2).recover_before_restart().action == "resume"


def _tamper_job(root: Path, job_id: str, **columns) -> None:
    with sqlite3.connect(root / "jobs.db") as connection:
        for column, value in columns.items():
            connection.execute(f"UPDATE durable_job SET {column}=? WHERE job_id=?", (value, job_id))


def _tamper_metadata(root: Path, job_id: str, **changes) -> None:
    with sqlite3.connect(root / "jobs.db") as connection:
        raw = connection.execute(
            "SELECT metadata_json FROM durable_job WHERE job_id=?", (job_id,),
        ).fetchone()[0]
        metadata = json.loads(raw)
        for key, value in changes.items():
            if value is None:
                metadata.pop(key, None)
            else:
                metadata[key] = value
        connection.execute(
            "UPDATE durable_job SET metadata_json=? WHERE job_id=?",
            (json.dumps(metadata), job_id),
        )


# An intent journaled under another worker, run or scope than the one the
# compute worker binds, with an otherwise valid ``compute-cancel`` operation id.
_FOREIGN_INTENT_BINDINGS = {
    "intent_worker": dict(worker_id="compute:other"),
    "intent_run_id": dict(run_id="runtime:other-jobs"),
    "intent_scope": dict(scope="other-jobs"),
}


def _foreign_binding(mismatch: str):
    fields = {
        "run_id": RUN_ID, "worker_id": f"compute:{WORKER_ID}", "scope": "compute-jobs",
        **_FOREIGN_INTENT_BINDINGS.get(mismatch, {}),
    }

    def factory(journal, epoch: int, *, auto: bool = False) -> AuthenticatedWorkerBinding:
        return AuthenticatedWorkerBinding(
            journal, fields["run_id"], fields["worker_id"], epoch, fields["scope"],
            auto_reconcile=auto,
        )

    return factory


@pytest.mark.parametrize("mismatch", [
    "digest", "legacy_missing_binding", "binding_for_other_attempt", "kind",
    "job_idempotency_key", "intent_idempotency_key", "worker", "controller",
    "unscoped", "strategy", *_FOREIGN_INTENT_BINDINGS,
])
def test_identity_or_digest_mismatch_stays_fenced(tmp_path, mismatch):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    job_id = _start_job(registry)
    if mismatch != "legacy_missing_binding":
        bound_key = _cancel_identity(job_id, 2 if mismatch == "binding_for_other_attempt" else 1)[1]
        bound_digest = (
            _request_digest(job_id, "another reason") if mismatch == "digest"
            else _request_digest(job_id)
        )
        registry.bind_cancel_request(
            job_id, idempotency_key=bound_key, request_digest=bound_digest,
        )
    _cancel_durably(registry, job_id)
    if mismatch == "kind":
        _tamper_job(tmp_path, job_id, kind="process")
    elif mismatch == "job_idempotency_key":
        _tamper_job(tmp_path, job_id, idempotency_key="other-submit")
    elif mismatch == "worker":
        _tamper_metadata(tmp_path, job_id, compute_worker_id="other-worker")
    elif mismatch == "controller":
        _tamper_metadata(tmp_path, job_id, compute_controller_job_id="other-controller")
    elif mismatch == "unscoped":
        _tamper_metadata(tmp_path, job_id, require_job_scope="0")
    journal = _journal(tmp_path)
    intent = _admit(
        journal, job_id,
        idempotency_key=(
            f"cancel:{job_id}:attempt-2" if mismatch == "intent_idempotency_key" else None
        ),
        reconciliation="manual" if mismatch == "strategy" else "idempotent",
        binding_factory=_foreign_binding(mismatch),
    )
    if mismatch in _FOREIGN_INTENT_BINDINGS:
        # Everything but the intent's own worker/run/scope is valid evidence.
        assert intent.operation_id == _cancel_identity(job_id)[0]
        assert intent.idempotency_key == _cancel_identity(job_id)[1]
        assert intent.request_digest == _request_digest(job_id)
    assert DurableComputeCancelVerifier(lambda: registry).verify(intent) is None
    _assert_fenced(journal, intent, _foreign_binding(mismatch))


@pytest.mark.parametrize("state", [
    "cancellation_requested", "running", "succeeded", "failed", "no_cleanup_evidence",
    "stale_cleanup_revision", "forged_cleanup_digest",
])
def test_non_terminal_or_uncleaned_state_stays_fenced(tmp_path, state):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    job_id = _start_job(registry)
    registry.bind_cancel_request(
        job_id, idempotency_key=_cancel_identity(job_id)[1],
        request_digest=_request_digest(job_id),
    )
    if state == "cancellation_requested":
        _cancel_durably(registry, job_id, clean=False)
    elif state in {"succeeded", "failed"}:
        registry.transition(job_id, JobStatus(state))
        _publish_cleanup(registry, job_id)
    elif state == "no_cleanup_evidence":
        registry.request_cancellation(job_id, reason=REASON)
        registry.cancel(job_id, reason=REASON)
    elif state == "stale_cleanup_revision":
        _cancel_durably(registry, job_id)
        _tamper_job(tmp_path, job_id, revision=registry.poll(job_id).revision + 1)
    elif state == "forged_cleanup_digest":
        registry.request_cancellation(job_id, reason=REASON)
        registry.cancel(job_id, reason=REASON)
        view = registry.view(job_id)
        forged = {
            "job_id": job_id, "job_revision": view.record.revision,
            "parent_session_id": view.record.identity.parent_session_id,
            "process_exited": True, "containment_empty": True,
            "resources_released": True, "status": "cancelled", "digest": "0" * 64,
        }
        registry._record_process_cleanup(job_id, forged)
    journal = _journal(tmp_path)
    intent = _admit(journal, job_id)
    assert DurableComputeCancelVerifier(lambda: registry).verify(intent) is None
    _assert_fenced(journal, intent)


@pytest.mark.parametrize("case", [
    "bound_after_terminal", "missing_bound_state", "bound_status_terminal",
    "bound_revision_not_older", "malformed_bound_state",
])
def test_binding_must_precede_the_cancelled_transition(tmp_path, case):
    """A cancel already complete by another path when bound is not this request's proof."""
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    job_id = _start_job(registry)
    key = _cancel_identity(job_id)[1]
    if case == "bound_after_terminal":
        # A hard deadline cancels and cleans the job first ...
        registry.request_cancellation(job_id, reason="process deadline exceeded")
        registry.cancel(job_id, reason="process deadline exceeded")
        _publish_cleanup(registry, job_id)
        revision = registry.poll(job_id).revision
        # ... and only then is the operator request bound.
        registry.bind_cancel_request(
            job_id, idempotency_key=key, request_digest=_request_digest(job_id),
        )
        assert registry.poll(job_id).revision == revision
        assert registry.view(job_id).metadata[CANCEL_REQUEST_BOUND_STATES][key] == {
            "revision": revision, "status": "cancelled",
        }
    else:
        registry.bind_cancel_request(
            job_id, idempotency_key=key, request_digest=_request_digest(job_id),
        )
        _cancel_durably(registry, job_id)
        cancelled_revision = registry.poll(job_id).revision
        forged = {
            "missing_bound_state": None,
            "bound_status_terminal": {key: {"revision": 1, "status": "cancelled"}},
            "bound_revision_not_older": {
                key: {"revision": cancelled_revision, "status": "running"},
            },
            "malformed_bound_state": {key: {"revision": "1", "status": ["running"]}},
        }[case]
        _tamper_metadata(tmp_path, job_id, **{CANCEL_REQUEST_BOUND_STATES: forged})
    journal = _journal(tmp_path)
    intent = _admit(journal, job_id)
    assert DurableComputeCancelVerifier(lambda: registry).verify(intent) is None
    _assert_fenced(journal, intent)


def test_missing_job_stays_fenced(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    journal = _journal(tmp_path)
    intent = _admit(journal, _job_id())
    assert DurableComputeCancelVerifier(lambda: registry).verify(intent) is None
    _assert_fenced(journal, intent)


def test_swapped_job_evidence_cannot_prove_another_jobs_cancel(tmp_path):
    """Job B's cleaned cancellation and binding never prove job A's cancel."""
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    job_a = _start_job(registry)
    job_b = _start_job(registry, OTHER_KEY)
    registry.bind_cancel_request(
        job_b, idempotency_key=_cancel_identity(job_b)[1],
        request_digest=_request_digest(job_b),
    )
    _cancel_durably(registry, job_b)
    journal = _journal(tmp_path)
    # A's cancel journaled with B's request digest (a swapped request) ...
    swapped_digest = _admit(journal, job_a, digest=_request_digest(job_b))
    verifier = DurableComputeCancelVerifier(lambda: registry)
    assert verifier.verify(swapped_digest) is None
    # ... and A's durable record replaced by B's content under A's id.
    with sqlite3.connect(tmp_path / "jobs.db") as connection:
        connection.execute("DELETE FROM durable_job WHERE job_id=?", (job_a,))
        connection.execute("UPDATE durable_job SET job_id=? WHERE job_id=?", (job_a, job_b))
    assert verifier.verify(swapped_digest) is None
    _assert_fenced(journal, swapped_digest)


@pytest.mark.parametrize("operation_id", [
    f"compute-cancel:{WORKER_ID}:attempt-1:{_job_id()}",
    f"compute-cancel:{WORKER_ID}:attempt-33:{_job_id()}",
    f"compute-cancel:{WORKER_ID}:attempt-02:{_job_id()}",
    f"compute-cancel:{WORKER_ID}:retry-2:{_job_id()}",
    f"compute-cancel:{WORKER_ID}",
    f"compute-cancel:{WORKER_ID}:x:y:{_job_id()}",
    f"compute-submit:{WORKER_ID}:{_job_id()}",
])
def test_malformed_operation_identities_produce_no_proof(tmp_path, operation_id):
    journal, registry, job_id, intent = _seed(tmp_path)
    from dataclasses import replace

    forged = replace(intent, operation_id=operation_id)
    assert DurableComputeCancelVerifier(lambda: registry).verify(forged) is None


def test_hung_registry_read_times_out_and_keeps_the_fence(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    job_id = _start_job(registry)
    registry.bind_cancel_request(
        job_id, idempotency_key=_cancel_identity(job_id)[1],
        request_digest=_request_digest(job_id),
    )
    _cancel_durably(registry, job_id)
    entered, release = threading.Event(), threading.Event()

    def hung_registry():
        entered.set()
        release.wait(5)
        return registry

    journal = _journal(tmp_path, hung_registry)
    intent = _admit(journal, job_id)
    with pytest.raises(EffectJournalError, match="reconciliation"):
        _binding(journal, 2).recover_before_restart()
    try:
        with pytest.raises(EffectJournalError, match="timed out"):
            journal.reconcile(intent.intent_id, owner_epoch=2, timeout_seconds=0.1)
        assert entered.is_set()
        assert journal.get(intent.intent_id).state is EffectState.UNCERTAIN
        with pytest.raises(EffectJournalError, match="reconciliation"):
            _binding(journal, 2).recover_before_restart()
    finally:
        release.set()
    # Once the provider answers, the same durable evidence proves the cancel.
    for _ in range(50):
        try:
            settled = journal.reconcile(intent.intent_id, owner_epoch=2, timeout_seconds=2)
            break
        except EffectJournalError as exc:  # the timed-out thread still holds its slot
            if "capacity" not in str(exc):
                raise
            threading.Event().wait(0.05)
    assert settled.state is EffectState.COMPLETED
    assert _binding(journal, 2).recover_before_restart().action == "resume"


@pytest.mark.parametrize("registry_kind", ["sqlite", "memory"])
def test_cancel_request_binding_is_write_once_bounded_and_revision_neutral(tmp_path, registry_kind):
    registry = (
        SQLiteDurableJobRegistry(tmp_path / "jobs.db") if registry_kind == "sqlite"
        else DurableJobRegistry()
    )
    job_id = _start_job(registry)
    revision = registry.poll(job_id).revision
    bound_status = registry.poll(job_id).status.value
    assert bound_status not in {"succeeded", "failed", "cancelled"}
    digest = _request_digest(job_id)
    registry.bind_cancel_request(job_id, idempotency_key=f"cancel:{job_id}", request_digest=digest)
    registry.bind_cancel_request(job_id, idempotency_key=f"cancel:{job_id}", request_digest=digest)
    assert registry.poll(job_id).revision == revision
    assert registry.view(job_id).metadata[CANCEL_REQUEST_DIGESTS] == {f"cancel:{job_id}": digest}
    assert registry.view(job_id).metadata[CANCEL_REQUEST_BOUND_STATES] == {
        f"cancel:{job_id}": {"revision": revision, "status": bound_status},
    }
    # A no-op rebind after a later transition keeps the originally bound state.
    registry.request_cancellation(job_id, reason=REASON)
    registry.bind_cancel_request(job_id, idempotency_key=f"cancel:{job_id}", request_digest=digest)
    assert registry.view(job_id).metadata[CANCEL_REQUEST_BOUND_STATES][f"cancel:{job_id}"] == {
        "revision": revision, "status": bound_status,
    }
    revision = registry.poll(job_id).revision
    with pytest.raises(ValueError, match="conflicts"):
        registry.bind_cancel_request(
            job_id, idempotency_key=f"cancel:{job_id}", request_digest="f" * 64,
        )
    for bad in ("", "F" * 64, "a" * 63):
        with pytest.raises(ValueError):
            registry.bind_cancel_request(job_id, idempotency_key="k", request_digest=bad)
    with pytest.raises(KeyError):
        registry.bind_cancel_request("cf-missing", idempotency_key="k", request_digest=digest)
    for index in range(2, MAX_CANCEL_REQUEST_BINDINGS + 1):
        registry.bind_cancel_request(
            job_id, idempotency_key=f"cancel:{job_id}:attempt-{index}", request_digest=digest,
        )
    with pytest.raises(ValueError, match="exhausted"):
        registry.bind_cancel_request(job_id, idempotency_key="one-more", request_digest=digest)
    assert registry.view(job_id).metadata[CANCEL_REQUEST_DIGESTS][f"cancel:{job_id}"] == digest


def _scope():
    from tests.test_job004_process_provider import _ScopedToken

    class _Scope(_ScopedToken):
        """Scripted forced quiesces; an emptied scope means the root exited."""

        def __init__(self) -> None:
            super().__init__()
            self.emptied = False

        def quiesce(self, *, force: bool) -> ProcessContainmentResult:
            self.calls.append(force)
            if force and self.results:
                return self.results.pop(0)
            if force:
                self.emptied = True
            return ProcessContainmentResult(True)

    return _Scope()


class _ManualTimer:
    """Deadline timer that only fires when a test asks it to."""

    armed: list = []

    def __init__(self, interval, function, args=()):
        self.function, self.args, self.daemon = function, args, True

    def start(self):
        _ManualTimer.armed.append(self)

    def cancel(self):
        return None


def _live_worker(root: Path, journal, epoch: int, scope):
    from tests.test_job004_process_provider import _Process, _ScopedLimiter
    from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor

    class _Running(_Process):
        """Runs until the containment scope is forcibly emptied."""

        def wait(self, timeout=None):
            import subprocess

            if not scope.emptied:
                raise subprocess.TimeoutExpired("sleeper", timeout or 0)
            return -9

        def poll(self):
            return -9 if scope.emptied else None

    registry = SQLiteDurableJobRegistry(root / "jobs.db")
    limiter = _ScopedLimiter(scope)
    provider = SubprocessJobProvider(
        registry, process_cleanup=ProcessTreeSupervisor(platform_name="posix"),
        memory_limiter=limiter, platform_name="posix",
        launcher=lambda *a, **kw: _Running(),
        process_identity_resolver=lambda _pid: "stable",
        timer_factory=_ManualTimer,
    )
    entry = JobCatalogEntry(
        entry_id="sleeper", workload=WorkloadKind.TEST, program="/bin/sleep",
        fixed_args=("60",), workspace_mappings=frozenset({"project"}),
    )
    return ComputeJobWorker(
        worker_id=WORKER_ID, catalog={"sleeper": entry},
        workspace_mappings={"project": root}, provider=provider,
        effect_binding=_binding(journal, epoch, auto=True),
    ), provider, registry


def _envelope() -> RemoteJobEnvelope:
    return RemoteJobEnvelope.create(
        controller_job_id="controller-job", idempotency_key=SUBMIT_KEY,
        workload=WorkloadKind.TEST, catalog_entry_id="sleeper", workspace_mapping="project",
    )


def test_live_cancel_binds_the_journaled_digest_before_the_provider_cancels(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ComputeJobWorker, "_artifact_stage_base",
        staticmethod(lambda: tmp_path / "artifact-stages"),
    )
    journal = _journal(tmp_path)
    scope = _scope()
    worker, provider, registry = _live_worker(tmp_path, journal, 1, scope)
    job_id = worker.submit(_envelope()).remote_job_id
    seen = []
    original_cancel = provider.cancel

    def observed_cancel(job, reason="cancelled"):
        seen.append(dict(registry.view(job).metadata.get(CANCEL_REQUEST_DIGESTS, {})))
        return original_cancel(job, reason)

    monkeypatch.setattr(provider, "cancel", observed_cancel)
    assert worker.cancel(job_id, REASON).state == "cancelled"
    intent = journal.get(f"{RUN_ID}:compute-cancel:{WORKER_ID}:{job_id}")
    assert intent.state is EffectState.COMPLETED
    assert seen == [{f"cancel:{job_id}": intent.request_digest}]
    assert registry.process_cleanup_proof(job_id) is not None


def _lose_receipt_after_cancel(monkeypatch, provider) -> None:
    """The provider cancel runs; the worker never gets its result back."""
    original = provider.cancel

    def cancel_then_lose(job_id, reason="cancelled"):
        original(job_id, reason)
        raise RuntimeError("receipt publication lost")

    monkeypatch.setattr(provider, "cancel", cancel_then_lose)


def test_cancel_crash_after_cleanup_is_proven_on_restart(tmp_path, monkeypatch):
    """The effect ran; its receipt did not publish.  Restart proves it."""
    monkeypatch.setattr(
        ComputeJobWorker, "_artifact_stage_base",
        staticmethod(lambda: tmp_path / "artifact-stages"),
    )
    journal = _journal(tmp_path)
    scope = _scope()
    worker, provider, registry = _live_worker(tmp_path, journal, 1, scope)
    job_id = worker.submit(_envelope()).remote_job_id

    _lose_receipt_after_cancel(monkeypatch, provider)
    with pytest.raises(RuntimeError, match="receipt publication lost"):
        worker.cancel(job_id, REASON)
    intent_id = f"{RUN_ID}:compute-cancel:{WORKER_ID}:{job_id}"
    assert journal.get(intent_id).state is EffectState.UNCERTAIN
    assert registry.poll(job_id).status is JobStatus.CANCELLED

    # A restarted worker (auto-reconciling binding, newer epoch) proves it.
    monkeypatch.undo()
    monkeypatch.setattr(
        ComputeJobWorker, "_artifact_stage_base",
        staticmethod(lambda: tmp_path / "artifact-stages"),
    )
    restarted, _, _ = _live_worker(tmp_path, journal, 2, _scope())
    settled = journal.get(intent_id)
    assert settled.state is EffectState.COMPLETED
    assert settled.receipt_key == f"{job_id}:cancelled"
    # A completed cancellation is not retried.
    with pytest.raises(EffectJournalError, match="already completed"):
        restarted.cancel(job_id, REASON)


def test_pending_cleanup_stays_fenced_until_the_provider_finishes_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ComputeJobWorker, "_artifact_stage_base",
        staticmethod(lambda: tmp_path / "artifact-stages"),
    )
    _ManualTimer.armed = []
    journal = _journal(tmp_path)
    scope = _scope()
    worker, provider, registry = _live_worker(tmp_path, journal, 1, scope)
    job_id = worker.submit(_envelope()).remote_job_id
    scope.results[:] = [ProcessContainmentResult(False, detail="descendant remains")]

    _lose_receipt_after_cancel(monkeypatch, provider)
    with pytest.raises(RuntimeError):
        worker.cancel(job_id, REASON)
    intent_id = f"{RUN_ID}:compute-cancel:{WORKER_ID}:{job_id}"
    assert registry.poll(job_id).status is JobStatus.CANCELLATION_REQUESTED
    successor = _binding(journal, 2, auto=True)
    with pytest.raises(EffectJournalError, match="reconciliation"):
        successor.recover_before_restart()
    assert journal.get(intent_id).state is EffectState.UNCERTAIN

    # The provider's own scheduled cleanup retry completes containment.
    retries = [timer for timer in _ManualTimer.armed if timer.args == (job_id,)]
    assert retries
    retries[-1].function(*retries[-1].args)
    assert registry.poll(job_id).status is JobStatus.CANCELLED
    assert registry.process_cleanup_proof(job_id) is not None
    assert successor.recover_before_restart().action == "resume"
    assert journal.get(intent_id).state is EffectState.COMPLETED


def test_cancel_of_a_job_already_cancelled_by_another_path_stays_fenced(tmp_path, monkeypatch):
    """A deadline cancel that finished first is not proof of the operator cancel.

    The live worker reports ``cancellation_requested`` for this request, not a
    cleaned ``cancelled``, so a restart must not settle it as ``cancelled``.
    """
    monkeypatch.setattr(
        ComputeJobWorker, "_artifact_stage_base",
        staticmethod(lambda: tmp_path / "artifact-stages"),
    )
    journal = _journal(tmp_path)
    worker, provider, registry = _live_worker(tmp_path, journal, 1, _scope())
    job_id = worker.submit(_envelope()).remote_job_id
    assert provider.cancel(job_id, "process deadline exceeded").cleanup_completed
    assert registry.poll(job_id).status is JobStatus.CANCELLED
    assert registry.process_cleanup_proof(job_id) is not None

    _lose_receipt_after_cancel(monkeypatch, provider)
    with pytest.raises(RuntimeError, match="receipt publication lost"):
        worker.cancel(job_id, REASON)
    intent_id = f"{RUN_ID}:compute-cancel:{WORKER_ID}:{job_id}"
    intent = journal.get(intent_id)
    assert intent.state is EffectState.UNCERTAIN
    metadata = registry.view(job_id).metadata
    assert metadata[CANCEL_REQUEST_DIGESTS] == {f"cancel:{job_id}": intent.request_digest}
    assert metadata[CANCEL_REQUEST_BOUND_STATES][f"cancel:{job_id}"]["status"] == "cancelled"
    assert DurableComputeCancelVerifier(lambda: registry).verify(intent) is None
    with pytest.raises(EffectJournalError, match="reconciliation"):
        _binding(journal, 2, auto=True).recover_before_restart()
    assert journal.get(intent_id).state is EffectState.UNCERTAIN
