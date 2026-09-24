from dataclasses import replace

import pytest

from sonder_runtime.adapters.persistence.sqlite.runtime_checkpoints import (
    SQLiteRuntimeCheckpointRepository,
)
from sonder_runtime.application.ports.runtime_checkpoints import (
    CheckpointConflict,
    CheckpointError,
    RuntimeCheckpoint,
)
from sonder_runtime.application.strategy.controller import StrategyController
from sonder_runtime.application.strategy.tracing import StrategyTraceService
from sonder_runtime.domain.strategy.models import (
    FailureClass,
    FailureObservation,
    ProgressMetric,
    ProgressVector,
    StrategyAction,
    StrategyAttempt,
    StrategyBudget,
    StrategyError,
    StrategySignature,
    StrategyUsage,
)


def repository(tmp_path):
    return SQLiteRuntimeCheckpointRepository(tmp_path / "state.db", seal_key=b"x" * 32)


def test_observation_survives_restart_without_replacing_checkpoint_facets(tmp_path):
    repo = repository(tmp_path)
    repo.save(RuntimeCheckpoint("run-1", 0, {"source": "runtime"},
                                routing={"code": "local"}, tool_state={"effect_high_water": 9}), expected_generation=-1)
    trace = StrategyTraceService(repo, StrategyController())
    first = trace.record(attempt(), budget=StrategyBudget(), available_actions=(StrategyAction.REPAIR, StrategyAction.CRITIC))
    assert first.action is StrategyAction.REPAIR
    restarted = StrategyTraceService(repository(tmp_path), StrategyController())
    second = restarted.record(attempt(2), budget=StrategyBudget(), available_actions=(StrategyAction.REPAIR, StrategyAction.CRITIC))
    assert second.action is StrategyAction.CRITIC
    checkpoint = repo.restore("run-1").checkpoint
    assert checkpoint.routing == {"code": "local"}
    assert checkpoint.tool_state == {"effect_high_water": 9}
    assert checkpoint.decisions["strategy_v1"]["mode"] == "observe"
    assert len(restarted.history("run-1")) == 2


def test_observation_replay_is_idempotent_and_conflicting_id_is_rejected(tmp_path):
    repo = repository(tmp_path)
    trace = StrategyTraceService(repo)
    kwargs = {"budget": StrategyBudget(), "available_actions": (StrategyAction.REPAIR,)}
    first = trace.record(attempt(), **kwargs)
    assert trace.record(attempt(), **kwargs) == first
    assert trace.record(attempt(), budget=StrategyBudget(), available_actions=()).action is StrategyAction.PAUSE
    assert trace.record(attempt(), unresolved_effects=True, **kwargs).action is StrategyAction.RECONCILE
    assert trace.record(attempt(), policy_blocked=True, **kwargs).action is StrategyAction.PAUSE
    assert trace.record(attempt(), artifacts_ready=False, **kwargs).action is StrategyAction.PAUSE
    assert repo.restore("run-1").checkpoint.generation == 0
    with pytest.raises(StrategyError):
        trace.record(replace(attempt(), model_route="different"), **kwargs)


def test_budget_cannot_expand_after_restart(tmp_path):
    trace = StrategyTraceService(repository(tmp_path))
    trace.record(attempt(), budget=StrategyBudget(attempts=2), available_actions=())
    with pytest.raises(StrategyError, match="budget"):
        trace.record(attempt(2), budget=StrategyBudget(attempts=3), available_actions=())


def test_replayed_attempt_rechecks_actions_and_persists_budget_reduction(tmp_path):
    repo = repository(tmp_path)
    trace = StrategyTraceService(repo)
    original = trace.record(attempt(), budget=StrategyBudget(attempts=4),
                            available_actions=(StrategyAction.REPAIR,))
    assert original.action is StrategyAction.REPAIR
    restarted = StrategyTraceService(repository(tmp_path))
    restricted = restarted.record(attempt(), budget=StrategyBudget(attempts=1),
                                  available_actions=())
    assert restricted.action is StrategyAction.FAIL
    saved = repo.restore("run-1").checkpoint
    assert saved.generation == 1
    assert saved.decisions["strategy_v1"]["budget"]["attempts"] == 1
    assert saved.decisions["strategy_v1"]["decisions"]["attempt-1"]["action"] == "repair"
    assert len(restarted.history("run-1")) == 1
    with pytest.raises(StrategyError, match="budget"):
        StrategyTraceService(repository(tmp_path)).record(
            attempt(2), budget=StrategyBudget(attempts=4), available_actions=())


def test_duplicate_old_attempt_cannot_override_later_failure(tmp_path):
    trace = StrategyTraceService(repository(tmp_path))
    trace.record(attempt(), budget=StrategyBudget(),
                 available_actions=(StrategyAction.REPAIR,))
    trace.record(attempt(2), budget=StrategyBudget(), available_actions=())
    duplicate = StrategyTraceService(repository(tmp_path)).record(
        attempt(), budget=StrategyBudget(), available_actions=(StrategyAction.REPAIR,))
    assert duplicate.action is StrategyAction.PAUSE
    assert len(trace.history("run-1")) == 2


@pytest.mark.parametrize("count", [0, 2])
def test_record_cannot_undercharge_or_bundle_durable_attempts(tmp_path, count):
    trace = StrategyTraceService(repository(tmp_path))
    with pytest.raises(StrategyError, match="exactly one"):
        trace.record(replace(attempt(), usage=StrategyUsage(attempts=count)),
                     budget=StrategyBudget(), available_actions=())
    assert trace.history("run-1") == ()


def test_restart_keeps_exhausted_attempt_budget(tmp_path):
    trace = StrategyTraceService(repository(tmp_path))
    trace.record(attempt(), budget=StrategyBudget(attempts=2),
                 available_actions=(StrategyAction.REPAIR,))
    decision = StrategyTraceService(repository(tmp_path)).record(
        attempt(2), budget=StrategyBudget(attempts=2),
        available_actions=(StrategyAction.REPAIR, StrategyAction.CRITIC))
    assert decision.action is StrategyAction.FAIL
    assert decision.reason == "strategy_budget_exhausted"


def test_strategy_write_uses_existing_checkpoint_compare_and_set(tmp_path):
    repo = repository(tmp_path)
    trace = StrategyTraceService(repo)
    original = repo.save
    def conflicting(checkpoint, *, expected_generation):
        original(RuntimeCheckpoint(checkpoint.run_id, checkpoint.generation, {"other_owner": True}),
                 expected_generation=expected_generation)
        return original(checkpoint, expected_generation=expected_generation)
    repo.save = conflicting
    with pytest.raises(CheckpointConflict):
        trace.record(attempt(), budget=StrategyBudget(), available_actions=())


def test_changed_seal_cannot_be_treated_as_empty_strategy_history(tmp_path):
    StrategyTraceService(repository(tmp_path)).record(attempt(), budget=StrategyBudget(), available_actions=())
    other_key = SQLiteRuntimeCheckpointRepository(tmp_path / "state.db", seal_key=b"y" * 32)
    with pytest.raises(CheckpointError):
        StrategyTraceService(other_key).history("run-1")


def attempt(number=1):
    measurement = ProgressVector("a" * 64, (ProgressMetric("errors", 2),))
    return StrategyAttempt(
        "run-1", f"attempt-{number}",
        StrategySignature("patch", "a" * 64, ("src/a.py",), "b" * 64, "repair", "tests"),
        "failed", FailureObservation(FailureClass.TEST_FAILURE), measurement, measurement,
        StrategyUsage(attempts=1, model_calls=1),
    )
