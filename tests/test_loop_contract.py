import pytest

from sonder_runtime.application.loop_contract import (
    InterceptionEvent, InterceptionPhase, StepContract, StepState,
    TurnContract, TurnState,
)


def test_turn_and_step_contracts_enforce_ordered_lifecycle():
    turn = TurnContract("turn_1").transition(TurnState.RUNNING)
    step = StepContract("step_1", turn.turn_id).transition(StepState.MODEL_REQUESTED)
    step = step.transition(StepState.EXECUTING).transition(StepState.COMPLETED)
    assert turn.state is TurnState.RUNNING
    assert step.state is StepState.COMPLETED
    with pytest.raises(ValueError):
        step.transition(StepState.EXECUTING)


def test_interception_contract_covers_retry_error_metadata():
    event = InterceptionEvent(InterceptionPhase.RETRY, "turn_1", "step_1", 1, "timeout")
    assert event.phase is InterceptionPhase.RETRY
    with pytest.raises(ValueError):
        InterceptionEvent(InterceptionPhase.ERROR, "turn_1")
