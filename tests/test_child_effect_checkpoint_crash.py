"""A real crash between the child checkpoint and enclosing runner receipt stays fenced."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

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
from sonder_runtime.application.subagents.durable_continuation import (
    DurableContinuationService,
)

CRASH_EXIT = 79
RUN_ID = "subagent:child-1"


def _old_worker(root: Path) -> None:
    from sonder_runtime.adapters.subagents import LocalSubagentProvider
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.execution.worker_bindings import journaled_effect
    from sonder_runtime.application.ports.subagents import (
        SubagentBudget,
        SubagentRequest,
    )

    child = SQLiteDurableContinuationRepository(root / "children.db")
    journal = SQLiteEffectJournal(root / "effects.db")
    service = DurableContinuationService(child)
    binding = AuthenticatedWorkerBinding(
        journal, RUN_ID, "subagent:worker-1", 1, "local-subagents",
    )
    context = local_owner_context(correlation_id="child-crash-matrix")
    budget = SubagentBudget(max_children=2, max_steps=4)

    def runner(_state, save, _control):
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
        assert journal.settled_high_water(RUN_ID) == 0
        os._exit(CRASH_EXIT)  # Child checkpoint committed; outer receipt did not.

    provider = LocalSubagentProvider(
        service, runner=runner, effect_binding_factory=lambda _request, _context: binding,
    )
    provider.register_root("root", budget, owner_id=context.principal_id)
    provider.spawn(
        SubagentRequest("root", "do work", budget, "child-1"), context,
    ).result(timeout=10)
    os._exit(78)  # A missed crash hook must not pass this test.


@pytest.mark.skipif(os.name != "posix", reason="real POSIX process crash probe")
def test_child_checkpoint_ahead_of_unsettled_outer_effect_remains_fenced(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(repo_root), os.environ.get("PYTHONPATH", ""))),
    }
    crashed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path)],
        cwd=repo_root, env=environment, capture_output=True, text=True,
        timeout=15, check=False,
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
    assert (page.high_water, page.settled_high_water) == (2, 0)
    outer, inner = page.records
    assert (outer.operation_id, outer.state) == (
        "subagent-run:child-1", EffectState.INTENT,
    )
    assert (inner.operation_id, inner.state, inner.receipt_key) == (
        "probe-write", EffectState.COMPLETED, "external-effect:1",
    )

    with pytest.raises(ContinuationCleanupRequired):
        DurableContinuationService(repository).recover_after_restart()
    successor = AuthenticatedWorkerBinding(
        journal, RUN_ID, "subagent:worker-1", 2, "local-subagents",
    )
    with pytest.raises(EffectJournalError, match="reconciliation"):
        successor.recover_before_restart()
    assert journal.get(outer.intent_id).state is EffectState.UNCERTAIN
    with pytest.raises(EffectJournalError):
        successor.binding().begin_request(
            operation_id="probe-write", idempotency_key="write-once",
            request_digest=inner.request_digest,
        )
    assert marker.read_bytes() == b"x"


if __name__ == "__main__":
    _old_worker(Path(sys.argv[1]))
