"""A recovery proposal must fit the remaining resources after restart."""
from dataclasses import replace

import pytest

from sonder_runtime.adapters.persistence.sqlite.runtime_checkpoints import (
    SQLiteRuntimeCheckpointRepository,
)
from sonder_runtime.application.strategy.controller import (
    StrategyController,
    StrategyState,
)
from sonder_runtime.application.strategy.tracing import StrategyTraceService
from sonder_runtime.domain.strategy.models import (
    FailureClass,
    FailureObservation,
    ProgressMetric,
    ProgressVector,
    StrategyAction,
    StrategyAttempt,
    StrategyBudget,
    StrategySignature,
    StrategyUsage,
)


def _attempt(number, *, succeeded=False):
    progress = ProgressVector("a" * 64, (ProgressMetric("errors", 2),))
    return StrategyAttempt(
        "resource-run", f"attempt-{number}",
        StrategySignature(
            "patch", "b" * 64, ("src/main.py",), "c" * 64,
            "repair an error", "tests",
        ),
        "succeeded" if succeeded else "failed",
        None if succeeded else FailureObservation(FailureClass.TEST_FAILURE),
        progress, progress, StrategyUsage(attempts=1, model_calls=1),
    )


@pytest.mark.parametrize(
    ("action", "budget", "usage"),
    [
        (StrategyAction.SPAWN_SPECIALIST,
         StrategyBudget(model_calls=2), StrategyUsage(attempts=2, model_calls=2)),
        (StrategyAction.SPAWN_SPECIALIST,
         StrategyBudget(tokens=0), StrategyUsage(attempts=2, model_calls=2)),
        (StrategyAction.SWITCH_TOOL,
         StrategyBudget(tool_calls=0), StrategyUsage(attempts=2, model_calls=2)),
        (StrategyAction.PARALLEL_HYPOTHESES,
         StrategyBudget(descendants=1), StrategyUsage(attempts=2, model_calls=2)),
        (StrategyAction.PARALLEL_HYPOTHESES,
         StrategyBudget(model_calls=3), StrategyUsage(attempts=2, model_calls=2)),
        (StrategyAction.PARALLEL_HYPOTHESES,
         StrategyBudget(tokens=1), StrategyUsage(attempts=2, model_calls=2)),
    ],
)
def test_stalled_recovery_requires_minimum_action_resources(action, budget, usage):
    state = StrategyState(
        objective_digest="b" * 64,
        history=(_attempt(1), _attempt(2)),
        failure=FailureObservation(FailureClass.TEST_FAILURE),
        budget=budget, usage=usage, available_actions=(action,),
    )
    decision = StrategyController().decide(state)
    assert decision.action is StrategyAction.PAUSE
    assert decision.reason == "no_admissible_strategy"


def test_exhausted_child_budget_stays_exhausted_after_checkpoint_restart(tmp_path):
    db = tmp_path / "checkpoints.db"

    def trace():
        return StrategyTraceService(
            SQLiteRuntimeCheckpointRepository(db, seal_key=b"x" * 32),
        )

    budget = StrategyBudget(model_calls=2, attempts=4)
    trace().record(
        _attempt(1), budget=budget,
        available_actions=(StrategyAction.SPAWN_SPECIALIST,),
    )
    decision = trace().record(
        _attempt(2), budget=budget,
        available_actions=(StrategyAction.SPAWN_SPECIALIST,),
    )
    assert decision.action is StrategyAction.PAUSE
    restarted = trace()
    assert len(restarted.history("resource-run")) == 2
    assert restarted.record(
        _attempt(2), budget=budget,
        available_actions=(StrategyAction.SPAWN_SPECIALIST,),
    ).action is StrategyAction.PAUSE


def test_final_success_at_exact_attempt_ceiling_awaits_completion_after_restart(tmp_path):
    db = tmp_path / "checkpoints.db"

    def trace():
        return StrategyTraceService(
            SQLiteRuntimeCheckpointRepository(db, seal_key=b"x" * 32),
        )

    successful = replace(_attempt(1, succeeded=True), usage=StrategyUsage(attempts=1, model_calls=1))
    budget = StrategyBudget(attempts=1, model_calls=1)
    initial = trace().record(successful, budget=budget, available_actions=())
    assert initial.action is StrategyAction.PAUSE
    assert initial.reason == "attempt_succeeded_await_completion_gate"
    replayed = trace().record(
        successful, budget=budget, available_actions=(StrategyAction.REPAIR,),
    )
    assert replayed == initial
