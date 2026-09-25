"""Bounded ``subagent-dispatch`` effect for local child admission (#515, LOOP-008).

The dispatch receipt proves durable admission of one exact child request.  It
does not wait for the runner, so inner effects completed during the run can
advance the settled journal high-water while the child is still running.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from sonder_runtime.adapters.execution.subagent_dispatch_verifier import (
    DurableSubagentDispatchVerifier,
    dispatch_receipt_key,
    dispatch_request_digest,
)
from sonder_runtime.adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
)
from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
    SQLiteEffectJournal,
)
from sonder_runtime.adapters.subagents import LocalSubagentProvider
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.execution import effect_journal
from sonder_runtime.application.execution.effect_journal import (
    EffectIntent,
    EffectJournalError,
    EffectState,
)
from sonder_runtime.application.execution.worker_bindings import (
    AuthenticatedWorkerBinding,
)
from sonder_runtime.application.ports.continuation_mutations import (
    ContinuationCleanupRequired,
)
from sonder_runtime.application.ports.continuation_records import (
    ChildSessionLineage,
    DurableChildSession,
)
from sonder_runtime.application.ports.subagents import (
    InvalidSubagentRequest,
    SubagentBudget,
    SubagentRequest,
    SubagentStatus,
)
from sonder_runtime.application.subagents.durable_continuation import (
    DurableContinuationService,
)

CRASH_EXIT = 83
CHILD = "child-1"
RUN_ID = f"subagent:{CHILD}"
WORKER = "subagent:worker-1"
SCOPE = "local-subagents"
OPERATION = f"subagent-dispatch:{CHILD}"
ROOT_BUDGET = SubagentBudget(max_children=4, max_steps=8, max_wall_seconds=60)


def _request(**changes) -> SubagentRequest:
    fields = {
        "parent_id": "root",
        "prompt": "bounded work",
        "budget": SubagentBudget(max_children=2, max_steps=4, max_wall_seconds=30),
        "child_id": CHILD,
        "metadata": (("purpose", "dispatch-test"),),
        "resume_key": "delegation-1",
        "idempotency_key": "delegation-1",
    }
    fields.update(changes)
    return SubagentRequest(**fields)


def _context(root: Path | None = None):
    return local_owner_context(
        correlation_id="dispatch-effect",
        workspace_roots=() if root is None else (root,),
    )


def _reusable_request(root: Path) -> SubagentRequest:
    """A request whose terminal result the service may return to its owner."""
    context = _context(root)
    return _request(metadata=(
        ("owner_id", context.principal_id),
        ("context_workspace_roots", str(root)),
        ("context_cloud_allowed", str(context.cloud_allowed)),
        ("context_remote_ollama_allowed", str(context.remote_ollama_allowed)),
        ("context_session_id", str(context.session_id)),
    ))


def _stores(root: Path, *, children: str = "children.db"):
    repository = SQLiteDurableContinuationRepository(root / children)
    verifier = DurableSubagentDispatchVerifier(lambda: repository)
    journal = SQLiteEffectJournal(
        root / "effects.db", reconciliation_verifiers={"subagent-dispatch": verifier},
    )
    return repository, verifier, journal


def _provider(repository, verifier, journal, runner, *, epoch: int = 1,
              recover: bool = False):
    binding = AuthenticatedWorkerBinding(journal, RUN_ID, WORKER, epoch, SCOPE)

    def factory(_request, _context):
        # Same shape as bootstrap ``_compose_subagent_binding``.
        if recover:
            binding.recover_before_restart()
        return binding

    service = DurableContinuationService(repository)
    provider = LocalSubagentProvider(
        service, runner, effect_binding_factory=factory, dispatch_verifier=verifier,
    )
    provider.register_root("root", ROOT_BUDGET, owner_id=_context().principal_id)
    return provider, binding, service


def test_dispatch_settles_before_runner_and_inner_effects_advance_high_water(tmp_path):
    repository, verifier, journal = _stores(tmp_path)
    at_start: list[tuple[str, EffectState]] = []
    inner_done, release = threading.Event(), threading.Event()

    def runner(_state, save, _control):
        page = journal.effects_since(RUN_ID, 0)
        at_start.extend((record.operation_id, record.state) for record in page.records)
        # The live gateway journals through the ambient binding; the dispatch
        # binding must be the one bound on the runner thread.
        bound = effect_journal.current()
        assert bound is not None and bound.run_id == RUN_ID
        intent = bound.begin_request(
            operation_id="write:notes.txt", idempotency_key="write-1",
            request_digest="a" * 64,
        )
        bound.complete(intent, outcome_digest="b" * 64, receipt_key="write:notes.txt:1")
        save({"phase": "after-write"}, "after-write")
        inner_done.set()
        assert release.wait(10)
        return "done"

    provider, _binding, _service = _provider(repository, verifier, journal, runner)
    request = _request()
    handle = provider.spawn(request, _context())
    assert inner_done.wait(10)
    try:
        assert at_start == [(OPERATION, EffectState.COMPLETED)]
        assert repository.get(CHILD).status is SubagentStatus.RUNNING
        page = journal.effects_since(RUN_ID, 0)
        assert [record.operation_id for record in page.records] == [
            OPERATION, "write:notes.txt",
        ]
        assert page.unresolved == ()
        assert journal.settled_high_water(RUN_ID) == 2
    finally:
        release.set()
    assert handle.result(timeout=10).status is SubagentStatus.SUCCEEDED

    dispatch = journal.get(f"{RUN_ID}:{OPERATION}")
    assert dispatch.state is EffectState.COMPLETED
    assert dispatch.reconciliation == "query"
    assert dispatch.idempotency_key == "delegation-1"
    assert dispatch.request_digest == dispatch_request_digest(request)
    admission = verifier.admission(
        CHILD, parent_id="root", idempotency_key="delegation-1",
        request_digest=dispatch.request_digest,
    )
    assert admission is not None and admission.admitted_revision >= 1
    assert dispatch.receipt_key == dispatch_receipt_key(CHILD, admission.admitted_revision)
    assert dispatch.outcome_digest == admission.outcome_digest
    # The receipt names admission, not runner completion: the durable row has
    # advanced past the admitted revision without changing the receipt.
    assert repository.get(CHILD).revision > admission.admitted_revision
    # Host reconciliation derives the same receipt from the durable store.
    proof = verifier.verify(dispatch)
    assert (proof.receipt_key, proof.outcome_digest) == (
        dispatch.receipt_key, dispatch.outcome_digest,
    )


def test_settled_dispatch_reuse_returns_the_admitted_child_without_second_runner(tmp_path):
    repository, verifier, journal = _stores(tmp_path)
    calls: list[str] = []

    def runner(_state, _save, _control):
        calls.append("run")
        return "done"

    provider, _binding, _service = _provider(repository, verifier, journal, runner)
    request, context = _reusable_request(tmp_path), _context(tmp_path)
    first = provider.spawn(request, context)
    assert first.result(timeout=10).status is SubagentStatus.SUCCEEDED
    again = provider.spawn(request, context)
    assert again.child_id == CHILD
    assert again.result(timeout=10).output == "done"
    assert calls == ["run"]
    dispatches = [
        record for record in journal.effects_since(RUN_ID, 0).records
        if record.operation_id == OPERATION
    ]
    assert len(dispatches) == 1

    # A settled receipt is not trusted from the journal alone: against a child
    # store that cannot prove the admission, reuse is refused before spawn.
    fresh = SQLiteDurableContinuationRepository(tmp_path / "fresh-children.db")
    fresh_provider = LocalSubagentProvider(
        DurableContinuationService(fresh), runner,
        effect_binding_factory=lambda *_: AuthenticatedWorkerBinding(
            journal, RUN_ID, WORKER, 1, SCOPE,
        ),
        dispatch_verifier=DurableSubagentDispatchVerifier(lambda: fresh),
    )
    fresh_provider.register_root("root", ROOT_BUDGET, owner_id=_context().principal_id)
    with pytest.raises(EffectJournalError, match="not provable"):
        fresh_provider.spawn(request, context)
    assert calls == ["run"]
    assert fresh.get(CHILD) is None


def test_synchronous_admission_refusal_is_a_failed_dispatch_not_a_fence(tmp_path):
    repository, verifier, journal = _stores(tmp_path)
    calls: list[str] = []
    provider, _binding, _service = _provider(
        repository, verifier, journal, lambda *_: calls.append("run") or "x",
    )
    too_wide = _request(budget=SubagentBudget(max_children=2, max_steps=99))
    with pytest.raises(InvalidSubagentRequest):
        provider.spawn(too_wide, _context())
    assert calls == [] and repository.get(CHILD) is None
    dispatch = journal.get(f"{RUN_ID}:{OPERATION}")
    assert dispatch.state is EffectState.FAILED
    assert dispatch.receipt_key == f"subagent-dispatch-refused:{CHILD}"
    successor = AuthenticatedWorkerBinding(journal, RUN_ID, WORKER, 2, SCOPE)
    assert successor.recover_before_restart().action == "resume"


def test_registry_reserved_delegation_dispatch_is_journaled_and_reusable(tmp_path):
    """Production shape: the worker registry creates the row before spawn."""
    from sonder_runtime.application.agents.delegation_service import DelegationService
    from sonder_runtime.application.agents.lineage_delegation import (
        DelegationRequest,
        LineageRecord,
        WorkspaceAssignment,
    )
    from sonder_runtime.application.agents.presets import resolve_preset
    from sonder_runtime.application.worker_registry.continuation import (
        ContinuationWorkerRegistry,
    )

    repository, verifier, journal = _stores(tmp_path)
    service = DurableContinuationService(repository)
    service.register_root(
        "root", SubagentBudget(max_steps=30, max_output_tokens=8000, max_wall_seconds=900),
    )
    calls: list[str] = []
    provider = LocalSubagentProvider(
        service, lambda *_: calls.append("run") or "delegated output",
        effect_binding_factory=lambda request, _context: AuthenticatedWorkerBinding(
            journal, f"subagent:{request.child_id}", WORKER, 1, SCOPE,
        ),
        dispatch_verifier=verifier,
    )
    delegation = DelegationService(
        provider, worker_registry=ContinuationWorkerRegistry(
            repository, owner_nonce=service.owner_nonce, owner_pid=service.owner_pid,
            owner_host=service.owner_host,
        ),
    )
    workspace = WorkspaceAssignment((str(tmp_path / "repo"),), ())
    preset = resolve_preset("researcher")
    lineage = LineageRecord(
        "line-1", "root", "root", CHILD, 1, preset.name, preset.role, workspace,
    )
    request = DelegationRequest("delegation-1", lineage, "do the work", preset, workspace)
    context = local_owner_context(
        correlation_id="delegation-1", workspace_roots=(tmp_path / "repo",),
    )
    first = delegation.dispatch(request, context).result(timeout=10)
    assert first.status is SubagentStatus.SUCCEEDED
    dispatch = journal.get(f"{RUN_ID}:{OPERATION}")
    assert dispatch.state is EffectState.COMPLETED
    assert dispatch.idempotency_key == "delegation-1"
    assert dispatch.request_digest == dispatch_request_digest(repository.get(CHILD).request)
    assert verifier.verify(dispatch).receipt_key == dispatch.receipt_key

    again = delegation.dispatch(request, context).result(timeout=10)
    assert again.output == "delegated output"
    assert calls == ["run"]


def test_binding_requires_a_durable_dispatch_verifier(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    with pytest.raises(TypeError, match="dispatch_verifier"):
        LocalSubagentProvider(
            DurableContinuationService(repository), lambda *_: "x",
            effect_binding_factory=lambda *_: None,
        )


# ---------------------------------------------------------------------------
# Verifier: no proof without an exact durable admission record.


def _admitted(tmp_path):
    repository, verifier, journal = _stores(tmp_path)
    provider, _binding, _service = _provider(
        repository, verifier, journal, lambda *_: "done",
    )
    request = _request()
    assert provider.spawn(request, _context()).result(timeout=10).status is (
        SubagentStatus.SUCCEEDED
    )
    return repository, verifier, journal, request


def _intent(request: SubagentRequest, **changes) -> EffectIntent:
    fields = {
        "intent_id": f"{RUN_ID}:{OPERATION}",
        "run_id": RUN_ID,
        "worker_id": WORKER,
        "operation_id": OPERATION,
        "scope": SCOPE,
        "owner_epoch": 1,
        "idempotency_key": request.idempotency_key,
        "request_digest": dispatch_request_digest(request),
        "reconciliation": "query",
        "state": EffectState.UNCERTAIN,
    }
    fields.update(changes)
    return EffectIntent(**fields)


def test_verifier_proves_only_the_exact_admitted_request(tmp_path):
    _repository, verifier, _journal, request = _admitted(tmp_path)
    proof = verifier.verify(_intent(request))
    assert proof is not None and proof.state is EffectState.COMPLETED
    assert proof.receipt_key.startswith(f"subagent-dispatch:{CHILD}:")
    assert proof.external_reference.startswith(f"child-store:{CHILD}:")


@pytest.mark.parametrize("case", [
    "missing-child-row", "request-digest-mismatch", "parent-mismatch",
    "different-idempotency-key", "wrong-run", "wrong-scope",
    "manual-reconciliation",
])
def test_verifier_returns_no_proof_for_mismatched_identity(tmp_path, case):
    repository, verifier, _journal, request = _admitted(tmp_path)
    if case == "missing-child-row":
        ghost = _request(child_id="ghost")
        intent = _intent(
            ghost, intent_id="subagent:ghost:subagent-dispatch:ghost",
            run_id="subagent:ghost", operation_id="subagent-dispatch:ghost",
        )
    elif case == "request-digest-mismatch":
        intent = _intent(request, request_digest="0" * 64)
    elif case == "parent-mismatch":
        intent = _intent(_request(parent_id="other-root"))
        assert verifier.admission(
            CHILD, parent_id="other-root", idempotency_key="delegation-1",
            request_digest=dispatch_request_digest(request),
        ) is None
    elif case == "different-idempotency-key":
        intent = _intent(request, idempotency_key="delegation-2")
    elif case == "wrong-run":
        intent = _intent(request, run_id="subagent:other")
    elif case == "wrong-scope":
        intent = _intent(request, scope="process-jobs")
    else:
        intent = _intent(request, reconciliation="manual")
    assert repository.get(CHILD) is not None
    assert verifier.verify(intent) is None


def _raw_status(path: Path, status: str) -> None:
    # Simulates a legacy or externally edited row: the status column changes
    # without any retained admission mutation in the store's log.
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE durable_child_session SET status=?,revision=revision+1 WHERE child_id=?",
            (status, CHILD),
        )


@pytest.mark.parametrize("status", ["created", "running", "succeeded"])
def test_verifier_refuses_legacy_row_without_retained_admission_record(tmp_path, status):
    repository, verifier, _journal = _stores(tmp_path)
    request = _request()
    DurableContinuationService(repository).register_root(
        "root", ROOT_BUDGET, owner_id=_context().principal_id,
    )
    repository.create(DurableChildSession(request, ChildSessionLineage("root")))
    if status != "created":
        _raw_status(tmp_path / "children.db", status)
    # Terminal status text alone is never proof of dispatch.
    assert repository.get(CHILD).status.value == status
    assert verifier.verify(_intent(request)) is None


def test_verifier_refuses_unstarted_cancelled_reservation(tmp_path):
    repository, verifier, _journal = _stores(tmp_path)
    request = _request()
    DurableContinuationService(repository).register_root(
        "root", ROOT_BUDGET, owner_id=_context().principal_id,
    )
    created = repository.create(DurableChildSession(request, ChildSessionLineage("root")))
    assert repository.request_cancel(
        CHILD, reason="never started", expected_revision=created.revision,
        unstarted_only=True,
    )
    assert verifier.verify(_intent(request)) is None


# ---------------------------------------------------------------------------
# Real interpreter crash after admission, before the dispatch receipt.


def _crash_before_receipt(root: Path) -> None:
    repository, verifier, journal = _stores(root)

    def runner(_state, _save, _control):
        (root / "runner-started").write_text("x", encoding="utf-8")
        return "must not run"

    provider, _binding, _service = _provider(repository, verifier, journal, runner)

    def crash(*_args, **_kwargs):
        os._exit(CRASH_EXIT)  # Admission committed; dispatch receipt did not.

    journal.outcome_and_checkpoint = crash
    provider.spawn(_request(), _context())
    os._exit(78)  # A missed crash hook must not pass this test.


@pytest.mark.skipif(os.name != "posix", reason="real POSIX process crash probe")
def test_crash_before_dispatch_receipt_is_fenced_and_reconciled_only_by_exact_row(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(repo_root), os.environ.get("PYTHONPATH", ""))),
    }
    crashed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path)],
        cwd=repo_root, env=environment, capture_output=True, text=True,
        timeout=60, check=False,
    )
    assert crashed.returncode == CRASH_EXIT, (crashed.returncode, crashed.stderr[-2000:])
    assert not (tmp_path / "runner-started").exists()

    repository, verifier, journal = _stores(tmp_path)
    child = repository.get(CHILD)
    assert child is not None and child.status is SubagentStatus.RUNNING
    stored = journal.get(f"{RUN_ID}:{OPERATION}")
    assert stored.state is EffectState.INTENT and stored.receipt_key == ""

    # Reopen at a newer epoch: the unproven dispatch fences the run and a
    # second spawn with the same idempotency key is refused before admission.
    calls: list[str] = []
    provider, successor, service = _provider(
        repository, verifier, journal, lambda *_: calls.append("run") or "x",
        epoch=2, recover=True,
    )
    with pytest.raises(EffectJournalError, match="reconciliation"):
        provider.spawn(_request(), _context())
    assert journal.get(stored.intent_id).state is EffectState.UNCERTAIN
    assert calls == []

    # A verifier reading a store without this exact row produces no proof.
    empty = SQLiteDurableContinuationRepository(tmp_path / "other-children.db")
    unrelated = SQLiteEffectJournal(
        tmp_path / "effects.db",
        reconciliation_verifiers={
            "subagent-dispatch": DurableSubagentDispatchVerifier(lambda: empty),
        },
    )
    with pytest.raises(EffectJournalError, match="no trusted proof"):
        unrelated.reconcile(stored.intent_id, owner_epoch=2)
    assert journal.get(stored.intent_id).state is EffectState.UNCERTAIN

    # The exact durable admission record settles it without running anything.
    reconciled = journal.reconcile(stored.intent_id, owner_epoch=2)
    assert reconciled.state is EffectState.COMPLETED
    assert reconciled.receipt_key.startswith(f"subagent-dispatch:{CHILD}:")
    assert successor.recover_before_restart().action == "resume"

    # The settled dispatch still cannot start a second runner: the admitted
    # child is running without an owner, so restart requires cleanup.
    with pytest.raises(InvalidSubagentRequest, match="recover/resume"):
        provider.spawn(_request(), _context())
    with pytest.raises(ContinuationCleanupRequired):
        service.recover_after_restart()
    assert calls == []
    assert not (tmp_path / "runner-started").exists()


if __name__ == "__main__":
    _crash_before_receipt(Path(sys.argv[1]))
