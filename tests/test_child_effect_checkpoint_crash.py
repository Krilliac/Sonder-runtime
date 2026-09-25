"""A real crash after the dispatch receipt, mid-run, stays non-resuming.

Semantics changed deliberately with the bounded ``subagent-dispatch`` effect
(#515 next-implementation item 1).  This test previously expected a
whole-runner ``subagent-run:{child_id}`` intent to stay unresolved beneath a
completed inner write, pinning the settled high-water at zero.  That
expectation is removed: the outer effect now covers admission only, so it is
``completed`` before the runner starts, and the inner write completed during
the run advances the settled high-water to 2 while the child checkpoint
exists.  What must not change is restart behaviour.  No bound child resume
adapter exists yet, so after the crash the running child requires owner
cleanup, the settled dispatch cannot start a second runner, and the inner
receipt is preserved rather than re-invoked.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from sonder_runtime.adapters.execution.subagent_dispatch_verifier import (
    DurableSubagentDispatchVerifier,
)
from sonder_runtime.adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
)
from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
    SQLiteEffectJournal,
)
from sonder_runtime.application.execution.effect_journal import (
    EffectJournalError,
    EffectState,
)
from sonder_runtime.application.execution.worker_bindings import (
    AuthenticatedWorkerBinding,
)
from sonder_runtime.application.ports.continuation_mutations import (
    ContinuationCleanupRequired,
)
from sonder_runtime.application.ports.subagents import InvalidSubagentRequest
from sonder_runtime.application.subagents.durable_continuation import (
    DurableContinuationService,
)

CRASH_EXIT = 79
RUN_ID = "subagent:child-1"
WORKER = "subagent:worker-1"


def _provider(root: Path, epoch: int, runner):
    from sonder_runtime.adapters.subagents import LocalSubagentProvider
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.ports.subagents import SubagentBudget

    child = SQLiteDurableContinuationRepository(root / "children.db")
    journal = SQLiteEffectJournal(root / "effects.db")
    service = DurableContinuationService(child)
    binding = AuthenticatedWorkerBinding(
        journal, RUN_ID, WORKER, epoch, "local-subagents",
    )

    def factory(_request, _context):
        # Same shape as bootstrap ``_compose_subagent_binding``.
        binding.recover_before_restart()
        return binding

    context = local_owner_context(correlation_id="child-crash-matrix")
    budget = SubagentBudget(max_children=2, max_steps=4)
    provider = LocalSubagentProvider(
        service, runner=runner, effect_binding_factory=factory,
        dispatch_verifier=DurableSubagentDispatchVerifier(lambda: child),
    )
    provider.register_root("root", budget, owner_id=context.principal_id)
    return provider, service, journal, context, budget


def _old_worker(root: Path) -> None:
    from sonder_runtime.application.execution.worker_bindings import journaled_effect
    from sonder_runtime.application.ports.subagents import SubagentRequest

    holder: dict[str, object] = {}

    def runner(_state, save, _control):
        journal = holder["journal"]
        binding = AuthenticatedWorkerBinding(
            journal, RUN_ID, WORKER, 1, "local-subagents",
        )

        def physical_write() -> str:
            with (root / "external-effect.log").open("ab", buffering=0) as output:
                output.write(b"x")
                os.fsync(output.fileno())
            return "physical-write-completed"

        journaled_effect(
            binding, operation_id="probe-write", idempotency_key="write-once",
            request={"input": "once"}, invoke=physical_write,
            receipt_key="external-effect:1",
        )
        saved = save({"phase": "after-write"}, "after-write")
        assert saved.sequence == 0
        assert journal.high_water(RUN_ID) == 2
        assert journal.settled_high_water(RUN_ID) == 2
        os._exit(CRASH_EXIT)  # Dispatch and inner receipt committed; run did not finish.

    provider, _service, journal, context, budget = _provider(root, 1, runner)
    holder["journal"] = journal
    provider.spawn(
        SubagentRequest("root", "do work", budget, "child-1", (), "task-1", "task-1"), context,
    ).result(timeout=10)
    os._exit(78)  # A missed crash hook must not pass this test.


@pytest.mark.skipif(os.name != "posix", reason="real POSIX process crash probe")
def test_crash_after_dispatch_receipt_preserves_inner_receipts_and_refuses_rerun(tmp_path):
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
    marker = tmp_path / "external-effect.log"
    assert marker.read_bytes() == b"x"

    repository = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    child = repository.get("child-1")
    assert child is not None and child.status.value == "running"
    assert child.checkpoint is not None and child.checkpoint.sequence == 0
    assert child.checkpoint.state == {"phase": "after-write"}
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    page = journal.effects_since(RUN_ID, 0)
    assert (page.high_water, page.settled_high_water) == (2, 2)
    dispatch, inner = page.records
    assert (dispatch.operation_id, dispatch.state) == (
        "subagent-dispatch:child-1", EffectState.COMPLETED,
    )
    assert dispatch.receipt_key.startswith("subagent-dispatch:child-1:")
    assert (inner.operation_id, inner.state, inner.receipt_key) == (
        "probe-write", EffectState.COMPLETED, "external-effect:1",
    )

    # No bound resume adapter exists: the running child needs owner cleanup.
    with pytest.raises(ContinuationCleanupRequired):
        DurableContinuationService(repository).recover_after_restart()

    # A restarted provider at a newer epoch finds nothing unresolved in the
    # journal, but the settled dispatch still cannot start a second runner.
    reran: list[str] = []
    provider, service, journal, context, budget = _provider(
        tmp_path, 2, lambda *_: reran.append("run") or "again",
    )
    from sonder_runtime.application.ports.subagents import SubagentRequest

    with pytest.raises(InvalidSubagentRequest, match="recover/resume"):
        provider.spawn(SubagentRequest("root", "do work", budget, "child-1", (), "task-1", "task-1"), context)
    with pytest.raises(ContinuationCleanupRequired):
        service.recover_after_restart()
    assert reran == []

    # Inner receipts are preserved: the same write is refused, not re-invoked.
    successor = AuthenticatedWorkerBinding(
        journal, RUN_ID, WORKER, 2, "local-subagents",
    )
    with pytest.raises(EffectJournalError):
        successor.binding().begin_request(
            operation_id="probe-write", idempotency_key="write-once",
            request_digest=inner.request_digest,
        )
    assert journal.get(inner.intent_id).state is EffectState.COMPLETED
    assert journal.get(dispatch.intent_id).state is EffectState.COMPLETED
    assert marker.read_bytes() == b"x"


if __name__ == "__main__":
    _old_worker(Path(sys.argv[1]))
