"""Hard-crash fault injection for every direct mutating worker family.

Issue #515 / LOOP-008 requires crash coverage before an effect, during an
effect, after an effect but before its receipt, and after the receipt but
before the checkpoint, using the live worker port and the persisted store.

Each case runs the real worker adapter (``SubprocessJobProvider``,
``ComputeJobWorker`` submit and cancel, ``LocalSubagentProvider``, and
``GuardedLegacySelfmodService`` deploy) in a child interpreter against a
file-backed ``SQLiteEffectJournal`` and terminates that interpreter with
``os._exit`` at one cut point.  No ``except``/``finally`` handler runs, so the
parent observes exactly what a killed worker leaves on disk.  The external
side effect is an append to a marker file, so duplicate execution is counted
rather than inferred.

The child exit status is asserted first: a case whose crash hook never fired
would otherwise look like a clean pass.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

REPO = Path(__file__).resolve().parents[1]
CRASH_EXIT = 86
FAMILIES = (
    "process-start", "compute-submit", "compute-cancel", "subagent-run",
    "selfmod-deploy",
)
CUTS = (
    "after_intent",     # intent committed, crash before the external effect
    "during_effect",    # effect performed, crash before the adapter returns
    "after_effect",     # adapter returned, crash before the receipt commit
    "in_receipt_txn",   # receipt applied, crash before checkpoint + COMMIT
    "after_commit",     # receipt + checkpoint committed, crash before return
)
# (run_id, worker_id, scope) per family; mirrors bootstrap composition shape.
IDENTITY = {
    "process-start": ("runtime:process-jobs", "process:crash-node", "process-jobs"),
    "compute-submit": ("runtime:compute-jobs", "compute:crash-node", "compute-jobs"),
    "compute-cancel": ("runtime:compute-jobs", "compute:crash-node", "compute-jobs"),
    "subagent-run": ("subagent:crash-child", "subagent:crash-node", "local-subagents"),
    "selfmod-deploy": ("selfmod:selfmod-test-1", "selfmod:crash-node", "selfmod-mutation"),
}


def _marker(root: Path) -> Path:
    return root / "external-effects.log"


def _effect_count(root: Path) -> int:
    marker = _marker(root)
    return len(marker.read_text(encoding="utf-8")) if marker.exists() else 0


def _append_effect(root: Path) -> None:
    with _marker(root).open("a", encoding="utf-8") as stream:
        stream.write("x")
        stream.flush()
        os.fsync(stream.fileno())


def _build(
    family: str, root: Path, epoch: int, effect: Callable[[], None],
    cancel_job_id: str = "unknown-job",
):
    """Construct the real worker adapter; return (journal, prepare, operate)."""
    from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
        SQLiteEffectJournal,
    )
    from sonder_runtime.application.execution.worker_bindings import (
        AuthenticatedWorkerBinding,
    )

    journal = SQLiteEffectJournal(root / "effects.db")
    run_id, worker_id, scope = IDENTITY[family]

    def binding() -> AuthenticatedWorkerBinding:
        return AuthenticatedWorkerBinding(journal, run_id, worker_id, epoch, scope)

    if family == "process-start":
        from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
        from sonder_runtime.application.jobs.durable_registry import DurableJobRegistry
        from tests.test_job004_process_provider import (
            _Cleanup, _MemoryLimiter, _Process, _request,
        )

        def launcher(*_args, **_kwargs):
            effect()
            return _Process()

        provider = SubprocessJobProvider(
            DurableJobRegistry(), process_cleanup=_Cleanup(complete=True),
            launcher=launcher, memory_limiter=_MemoryLimiter(),
            process_identity_resolver=lambda _pid: "stable",
            platform_name="posix", effect_binding=binding(),
        )
        return journal, (lambda: None), (lambda: provider.start(_request("crash-job")))

    if family in {"compute-submit", "compute-cancel"}:
        from sonder_runtime.application.compute_fabric.jobs import ComputeJobWorker
        from tests.test_compute_job_worker import CapturingProvider, _entry, _envelope

        ComputeJobWorker._artifact_stage_base = staticmethod(
            lambda: root / "artifact-stages"
        )

        class EffectProvider(CapturingProvider):
            def start(self, request):
                if family == "compute-submit":
                    effect()
                return super().start(request)

            def cancel(self, job_id, reason="cancelled"):
                effect()
                return super().cancel(job_id, reason)

        worker = ComputeJobWorker(
            worker_id="compute-worker", catalog={"pytest": _entry()},
            workspace_mappings={"sonder": root}, provider=EffectProvider(),
            effect_binding=binding(),
        )
        if family == "compute-submit":
            return journal, (lambda: None), (lambda: worker.submit(_envelope()))
        submitted: list[str] = []

        def prepare() -> None:
            submitted.append(worker.submit(_envelope()).remote_job_id)

        def cancel():
            job_id = submitted[0] if submitted else cancel_job_id
            return worker.cancel(job_id, "operator cancel")

        return journal, prepare, cancel

    if family == "subagent-run":
        from sonder_runtime.adapters.persistence.durable_continuation import (
            SQLiteDurableContinuationRepository,
        )
        from sonder_runtime.adapters.subagents import LocalSubagentProvider
        from sonder_runtime.application.context import local_owner_context
        from sonder_runtime.application.ports.subagents import (
            SubagentBudget, SubagentRequest,
        )
        from sonder_runtime.application.subagents.durable_continuation import (
            DurableContinuationService,
        )

        def factory(_request, _context):
            # Same shape as bootstrap ``_compose_subagent_binding``.
            composed = binding()
            composed.recover_before_restart()
            return composed

        def runner(_state, _save, _control):
            effect()
            return "child result"

        # A fresh continuation store per epoch: only the effect journal may
        # stop a post-restart duplicate, not continuation bookkeeping.
        service = DurableContinuationService(
            SQLiteDurableContinuationRepository(root / f"children-{epoch}.db")
        )
        provider = LocalSubagentProvider(service, runner, effect_binding_factory=factory)
        provider.register_root(
            "root-1", SubagentBudget(max_steps=8, max_output_tokens=100, max_wall_seconds=30),
        )
        request = SubagentRequest(
            "root-1", "bounded work",
            SubagentBudget(max_steps=4, max_wall_seconds=10, max_output_tokens=20),
            "crash-child", (("role", "explorer"),),
        )

        def spawn():
            handle = provider.spawn(request, local_owner_context(correlation_id="crash"))
            return handle.result(timeout=20)

        return journal, (lambda: None), spawn

    if family == "selfmod-deploy":
        from sonder_runtime.application.selfmod.selfmod_service import (
            GuardedLegacySelfmodService,
        )
        from tests.test_selfmod_legacy_integration import LegacyDouble

        class EffectLegacy(LegacyDouble):
            def deploy(self, run_id, **kwargs):
                effect()
                return super().deploy(run_id, **kwargs)

        def factory(_run_id):
            # Same shape as bootstrap ``_compose_selfmod_binding``.
            composed = binding()
            composed.recover_before_restart()
            return composed

        service = GuardedLegacySelfmodService(
            EffectLegacy(), unrestricted=True, effect_binding_factory=factory,
        )
        service.create_plan("change", root)
        return journal, (lambda: None), (lambda: service.deploy("selfmod-test-1", commit=False))

    raise AssertionError(family)


def _child(family: str, cut: str, root: Path) -> None:
    """Run one operation and hard-kill this interpreter at ``cut``."""
    armed = False

    def crash() -> None:
        os._exit(CRASH_EXIT)

    def effect() -> None:
        if armed and cut == "after_intent":
            crash()
        _append_effect(root)
        if armed and cut == "during_effect":
            crash()

    journal, prepare, operate = _build(family, root, 1, effect)
    prepare()
    baseline = _effect_count(root)
    (root / "baseline.txt").write_text(str(baseline), encoding="utf-8")
    armed = True
    original = journal.outcome_and_checkpoint
    if cut == "after_effect":
        journal.outcome_and_checkpoint = lambda *_a, **_k: crash()
    elif cut == "in_receipt_txn":
        # The outcome UPDATE has executed inside BEGIN IMMEDIATE; die before
        # the checkpoint insert and COMMIT.
        journal._append_checkpoint_in_transaction = lambda *_a, **_k: crash()
    elif cut == "after_commit":
        def commit_then_crash(*args, **kwargs):
            original(*args, **kwargs)
            crash()
        journal.outcome_and_checkpoint = commit_then_crash
    operate()
    os._exit(0)  # the crash hook did not fire; the parent treats this as failure


def _latest_intent(db: Path, run_id: str, family: str):
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT intent_id FROM effect_journal WHERE run_id=? AND operation_id LIKE ? "
            "ORDER BY sequence DESC LIMIT 1",
            (run_id, family + ":%"),
        ).fetchone()
    return None if row is None else str(row[0])


def _checkpoint_high_water(db: Path, run_id: str) -> int:
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(effect_high_water),-1) FROM effect_checkpoint WHERE run_id=?",
            (run_id,),
        ).fetchone()
    return int(row[0])


@pytest.mark.parametrize("cut", CUTS)
@pytest.mark.parametrize("family", FAMILIES)
def test_hard_crash_never_duplicates_or_falsely_completes(family, cut, tmp_path):
    from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
        SQLiteEffectJournal,
    )
    from sonder_runtime.application.execution.effect_journal import (
        EffectJournalError, EffectOutcome, EffectState,
    )
    from sonder_runtime.application.execution.worker_bindings import (
        AuthenticatedWorkerBinding,
    )
    from sonder_runtime.application.ports.subagents import SubagentStatus

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(REPO), env.get("PYTHONPATH"))))
    env["SONDER_STATE_HOME"] = str(tmp_path / "state")
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), family, cut, str(tmp_path)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
    )
    # A child that finished normally or failed elsewhere never reached the cut.
    assert completed.returncode == CRASH_EXIT, (completed.returncode, completed.stderr[-4000:])

    baseline = int((tmp_path / "baseline.txt").read_text(encoding="utf-8"))
    expected_effects = baseline + (0 if cut == "after_intent" else 1)
    assert _effect_count(tmp_path) == expected_effects

    db = tmp_path / "effects.db"
    run_id, worker_id, scope = IDENTITY[family]
    intent_id = _latest_intent(db, run_id, family)
    assert intent_id is not None, "the intent must be durable before the effect"
    journal = SQLiteEffectJournal(db)
    stored = journal.get(intent_id)

    if cut == "after_commit":
        # Receipt and checkpoint are durable together.
        assert stored.state is EffectState.COMPLETED
        assert _checkpoint_high_water(db, run_id) == stored.sequence
        decision = AuthenticatedWorkerBinding(
            journal, run_id, worker_id, 2, scope,
        ).recover_before_restart()
        assert decision.action == "resume"
        restored = journal.restore_checkpoint(run_id)
        assert restored["effect_high_water"] == journal.high_water(run_id)
        assert restored["state"]["effect"]["receipt_key"] == stored.receipt_key
    else:
        # A hard crash leaves an admitted intent with no receipt and no
        # checkpoint covering it -- including when the receipt UPDATE had
        # already executed inside the uncommitted transaction.
        assert stored.state is EffectState.INTENT
        assert stored.receipt_key == ""
        prior_checkpoint = _checkpoint_high_water(db, run_id)
        assert prior_checkpoint < stored.sequence
        if prior_checkpoint >= 0:
            # An older checkpoint exists but cannot authorize replay.
            with pytest.raises(EffectJournalError):
                journal.restore_checkpoint(run_id)
        else:
            assert journal.restore_checkpoint(run_id) is None
        with pytest.raises(ValueError, match="reconciliation"):
            AuthenticatedWorkerBinding(
                journal, run_id, worker_id, 2, scope,
            ).recover_before_restart()
        assert journal.get(intent_id).state is EffectState.UNCERTAIN
        # The crashed owner's late receipt cannot complete the effect.
        with pytest.raises(EffectJournalError):
            journal.outcome(EffectOutcome(
                intent_id, EffectState.COMPLETED, "d" * 64, "late-receipt",
                worker_id=worker_id, owner_epoch=1,
            ))
        assert journal.get(intent_id).state is EffectState.UNCERTAIN

    # A restarted worker retrying the same operation must not re-run it.
    retried: list[str] = []

    def retry_effect() -> None:
        retried.append("x")
        _append_effect(tmp_path)

    cancel_job_id = stored.operation_id.rsplit(":", 1)[-1]
    outcome = refusal = None
    try:
        _, _prepare, operate = _build(
            family, tmp_path, 3, retry_effect, cancel_job_id=cancel_job_id,
        )
        outcome = operate()
    except Exception as exc:  # noqa: BLE001 - classified below
        refusal = exc
    if family == "subagent-run":
        # The child runs on a worker thread; the refusal surfaces as a
        # failed child rather than an exception from spawn().
        assert refusal is None and outcome is not None
        assert outcome.status is not SubagentStatus.SUCCEEDED
    else:
        # The refusal must come from the effect journal (restart fence or
        # duplicate-intent refusal), not from an unrelated fixture error.
        assert isinstance(refusal, EffectJournalError), repr(refusal)
    assert retried == []
    assert _effect_count(tmp_path) == expected_effects
    final = journal.get(intent_id)
    assert final.state is (
        EffectState.COMPLETED if cut == "after_commit" else EffectState.UNCERTAIN
    )


def test_post_invoke_publication_failure_is_uncertain_not_reattachable(tmp_path):
    """An effect that ran must never stay a reattachable bare intent."""
    from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
        SQLiteEffectJournal,
    )
    from sonder_runtime.application.execution.effect_journal import (
        EffectJournalError, EffectState,
    )
    from sonder_runtime.application.execution.worker_bindings import (
        AuthenticatedWorkerBinding, journaled_effect,
    )

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    binding = AuthenticatedWorkerBinding(journal, "run", "worker", 1, "/workspace")
    performed = []
    with pytest.raises(EffectJournalError):
        journaled_effect(
            binding, operation_id="op", idempotency_key="op-1", request={"a": 1},
            invoke=lambda: performed.append("x") or "ok", receipt_key="receipt",
            checkpoint_state=lambda _result: object(),  # not serializable
        )
    assert performed == ["x"]
    stored = journal.get("run:op")
    assert stored.state is EffectState.UNCERTAIN
    decision = journal.recover("run", live_workers={"worker": 1})
    assert decision.action == "reconcile"


def test_success_predicate_failure_after_effect_is_uncertain(tmp_path):
    from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
        SQLiteEffectJournal,
    )
    from sonder_runtime.application.execution.effect_journal import EffectState
    from sonder_runtime.application.execution.worker_bindings import (
        AuthenticatedWorkerBinding, journaled_effect,
    )

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    binding = AuthenticatedWorkerBinding(journal, "run", "worker", 1, "/workspace")

    def broken_success(_result):
        raise KeyError("phase")

    with pytest.raises(KeyError):
        journaled_effect(
            binding, operation_id="op", idempotency_key="op-1", request={},
            invoke=lambda: {"done": True}, receipt_key="receipt",
            success=broken_success,
        )
    assert journal.get("run:op").state is EffectState.UNCERTAIN


if __name__ == "__main__":
    _child(sys.argv[1], sys.argv[2], Path(sys.argv[3]))
