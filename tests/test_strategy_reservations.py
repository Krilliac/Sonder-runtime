"""Active canary model work is sealed before dispatch and closes once."""
from dataclasses import replace

import pytest

from sonder_runtime.adapters.persistence.sqlite.runtime_checkpoints import (
    SQLiteRuntimeCheckpointRepository,
)
from sonder_runtime.application.ports.runtime_checkpoints import CheckpointError
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

OBJECTIVE = "a" * 64
USAGE = StrategyUsage(
    attempts=1, model_calls=2, tool_calls=2, verifier_calls=1,
    tokens=32, critic_calls=1, top_tier_calls=2,
)
BUDGET = StrategyBudget(
    attempts=3, model_calls=6, tool_calls=6, verifier_calls=3,
    tokens=96, critic_calls=3, top_tier_calls=6,
)


def _trace(tmp_path):
    return StrategyTraceService(
        SQLiteRuntimeCheckpointRepository(tmp_path / "state.db", seal_key=b"x" * 32),
    )


def _attempt(usage=USAGE):
    measured = ProgressVector(
        "b" * 64, (ProgressMetric("compiler_errors", 1),), complete=True,
    )
    return StrategyAttempt(
        "run-1", "attempt-1",
        StrategySignature("patch", OBJECTIVE, ("project:123",), "c" * 64,
                          "repair compiled file", "build:123"),
        "failed", FailureObservation(FailureClass.BUILD_FAILURE),
        measured, measured, usage,
    )


def _reserve(trace):
    trace.reserve_next(
        "run-1", "attempt-1", objective_digest=OBJECTIVE,
        action=StrategyAction.CRITIC, usage=USAGE, budget=BUDGET,
    )


def _complete(trace, attempt=None):
    return trace.record_reserved(
        attempt or _attempt(), budget=BUDGET, action=StrategyAction.CRITIC,
        available_actions=(StrategyAction.REPAIR,),
    )


def test_pending_reservation_survives_restart_and_blocks_unreserved_work(tmp_path):
    trace = _trace(tmp_path)
    _reserve(trace)
    restarted = _trace(tmp_path)
    pending = restarted.pending("run-1")
    assert pending["mode"] == "active_canary"
    assert pending["attempt_id"] == "attempt-1"
    assert pending["usage"]["model_calls"] == 2
    assert restarted.history("run-1") == ()
    with pytest.raises(CheckpointError, match="unresolved strategy reservation"):
        restarted.record(
            _attempt(), budget=BUDGET,
            available_actions=(StrategyAction.REPAIR,),
        )
    with pytest.raises(CheckpointError, match="unresolved strategy reservation"):
        restarted.reserve_next(
            "run-1", "attempt-2", objective_digest=OBJECTIVE,
            action=StrategyAction.REPAIR, usage=USAGE, budget=BUDGET,
        )
    assert _complete(restarted).action is StrategyAction.REPAIR
    assert _trace(tmp_path).pending("run-1") is None
    assert len(_trace(tmp_path).history("run-1")) == 1
    assert _complete(_trace(tmp_path)).action is StrategyAction.REPAIR
    assert len(_trace(tmp_path).history("run-1")) == 1


def test_reservation_cannot_complete_other_identity_or_exceed_charged_usage(tmp_path):
    trace = _trace(tmp_path)
    _reserve(trace)
    with pytest.raises(CheckpointError, match="reservation identity"):
        _complete(trace, replace(_attempt(), attempt_id="attempt-2"))
    with pytest.raises(CheckpointError, match="reservation identity"):
        _complete(trace, replace(
            _attempt(), signature=replace(_attempt().signature, objective_digest="d" * 64),
        ))
    with pytest.raises(StrategyError, match="reservation usage"):
        _complete(trace, _attempt(replace(USAGE, model_calls=3)))
    with pytest.raises(CheckpointError, match="reservation action"):
        trace.record_reserved(
            _attempt(), budget=BUDGET, action=StrategyAction.REPAIR,
            available_actions=(StrategyAction.REPAIR,),
        )
    assert _trace(tmp_path).pending("run-1") is not None
    assert _complete(trace).action is StrategyAction.REPAIR
    with pytest.raises(CheckpointError, match="already completed"):
        trace.reserve_next(
            "run-1", "attempt-1", objective_digest=OBJECTIVE,
            action=StrategyAction.CRITIC, usage=USAGE, budget=BUDGET,
        )


def test_reservation_rejects_budget_overrun_before_paid_call(tmp_path):
    trace = _trace(tmp_path)
    with pytest.raises(StrategyError, match="reservation exceeds"):
        trace.reserve_next(
            "run-1", "attempt-1", objective_digest=OBJECTIVE,
            action=StrategyAction.CRITIC, usage=USAGE,
            budget=replace(BUDGET, top_tier_calls=1),
        )
    assert trace.pending("run-1") is None


def test_completion_write_failure_keeps_pending_crash_cut(tmp_path):
    trace = _trace(tmp_path)
    _reserve(trace)
    repository = trace._repository
    original = repository.save

    def fail_completion(*args, **kwargs):
        raise OSError("simulated crash before completion checkpoint")

    repository.save = fail_completion
    with pytest.raises(OSError, match="simulated crash"):
        _complete(trace)
    repository.save = original
    assert _trace(tmp_path).pending("run-1") is not None
    assert _trace(tmp_path).history("run-1") == ()


def test_project_guard_cannot_release_with_forged_member_list_or_pending_child(tmp_path):
    trace = _trace(tmp_path)
    trace.acquire_scope_guard(
        "project-scope", "owner-1", objective_digest=OBJECTIVE,
        member_run_ids=("run-1", "run-2"),
    )
    _reserve(trace)
    with pytest.raises(CheckpointError, match="owner mismatch"):
        trace.release_scope_guard("project-scope", "owner-1", member_run_ids=())
    with pytest.raises(CheckpointError, match="owner mismatch"):
        trace.release_scope_guard("project-scope", "owner-1", member_run_ids=("run-2",))
    with pytest.raises(CheckpointError, match="unresolved strategy reservation"):
        trace.release_scope_guard(
            "project-scope", "owner-1", member_run_ids=("run-1", "run-2"),
        )
    assert _trace(tmp_path).scope_guard("project-scope")["owner_run_id"] == "owner-1"
    _complete(trace)
    trace.release_scope_guard(
        "project-scope", "owner-1", member_run_ids=("run-1", "run-2"),
    )
    assert _trace(tmp_path).scope_guard("project-scope") is None


def test_project_guard_acquisition_is_exclusive_and_survives_restart(tmp_path):
    trace = _trace(tmp_path)
    trace.acquire_scope_guard(
        "project-scope", "owner-1", objective_digest=OBJECTIVE,
        member_run_ids=("run-1",),
    )
    restarted = _trace(tmp_path)
    with pytest.raises(CheckpointError, match="project guard"):
        restarted.acquire_scope_guard(
            "project-scope", "owner-2", objective_digest=OBJECTIVE,
            member_run_ids=("run-2",),
        )
