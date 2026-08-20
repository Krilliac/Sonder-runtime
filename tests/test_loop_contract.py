"""WP2 LOOP-001/002/003: immutable lifecycle and interception contracts."""

import pytest

from sonder_runtime.application.loop_contract import (
    InterceptionEvent,
    InterceptionPhase,
    StepContract,
    StepState,
    TurnContract,
    TurnState,
)


def test_turn_ends_only_after_owed_steps_reconcile():
    turn = TurnContract("turn-1").transition(TurnState.RUNNING).owe_step()
    with pytest.raises(ValueError):
        turn.complete()
    finished = turn.reconcile_step().transition(TurnState.STOPPING).complete()
    assert finished.state is TurnState.COMPLETED
    assert turn.state is TurnState.RUNNING


def test_step_is_one_model_request_and_all_tool_results():
    step = StepContract("step-1", "turn-1").transition(StepState.MODEL_REQUESTED)
    finished = step.transition(StepState.EXECUTING).transition(StepState.COMPLETED)
    assert finished.state is StepState.COMPLETED
    assert finished.attempt == 1


def test_retry_is_an_explicit_new_attempt_and_snapshots_are_immutable():
    step = StepContract("step-1", "turn-1").transition(StepState.MODEL_REQUESTED).transition(StepState.FAILED)
    retried = step.retry()
    assert retried.state is StepState.RETRYING and retried.attempt == 2
    assert step.state is StepState.FAILED


def test_interception_phases_are_typed_and_validate_step_scoped_phases():
    assert [phase.value for phase in InterceptionPhase] == [
        "pre_step", "model_request", "pre_execute", "execute", "post_execute",
        "turn_stopping", "error", "retry",
    ]
    with pytest.raises(ValueError):
        InterceptionEvent(InterceptionPhase.ERROR, "turn-1")
    event = InterceptionEvent(InterceptionPhase.ERROR, "turn-1", error_code="timeout")
    assert event.error_code == "timeout"
