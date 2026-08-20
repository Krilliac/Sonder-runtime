"""Typed turn/step lifecycle and interception contracts for WP2."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class TurnState(str, Enum):
    ADMITTED = "admitted"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepState(str, Enum):
    CREATED = "created"
    MODEL_REQUESTED = "model_requested"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"
    CANCELLED = "cancelled"


class InterceptionPhase(str, Enum):
    PRE_STEP = "pre_step"
    MODEL_REQUEST = "model_request"
    PRE_EXECUTE = "pre_execute"
    EXECUTE = "execute"
    POST_EXECUTE = "post_execute"
    TURN_STOPPING = "turn_stopping"
    ERROR = "error"
    RETRY = "retry"


_TURN_TRANSITIONS = {
    TurnState.ADMITTED: {TurnState.RUNNING, TurnState.CANCELLED},
    TurnState.RUNNING: {TurnState.STOPPING, TurnState.COMPLETED, TurnState.FAILED, TurnState.CANCELLED},
    TurnState.STOPPING: {TurnState.COMPLETED, TurnState.FAILED, TurnState.CANCELLED},
    TurnState.COMPLETED: set(), TurnState.FAILED: set(), TurnState.CANCELLED: set(),
}
_STEP_TRANSITIONS = {
    StepState.CREATED: {StepState.MODEL_REQUESTED, StepState.CANCELLED},
    StepState.MODEL_REQUESTED: {StepState.EXECUTING, StepState.COMPLETED, StepState.FAILED, StepState.RETRYING, StepState.CANCELLED},
    StepState.EXECUTING: {StepState.COMPLETED, StepState.FAILED, StepState.CANCELLED},
    StepState.COMPLETED: set(), StepState.FAILED: {StepState.RETRYING},
    StepState.RETRYING: {StepState.MODEL_REQUESTED, StepState.CANCELLED},
    StepState.CANCELLED: set(),
}


@dataclass(frozen=True)
class TurnContract:
    turn_id: str
    state: TurnState = TurnState.ADMITTED
    accepted_steering: int = 0
    owed_steps: int = 0

    def transition(self, state: TurnState) -> "TurnContract":
        state = TurnState(state)
        if state not in _TURN_TRANSITIONS[self.state]:
            raise ValueError(f"invalid turn transition {self.state.value}->{state.value}")
        return replace(self, state=state)

    def owe_step(self) -> "TurnContract":
        if self.state not in {TurnState.ADMITTED, TurnState.RUNNING}:
            raise ValueError("cannot add work to a non-active turn")
        return replace(self, owed_steps=self.owed_steps + 1)

    def reconcile_step(self) -> "TurnContract":
        if self.owed_steps == 0:
            raise ValueError("turn has no owed steps")
        return replace(self, owed_steps=self.owed_steps - 1)

    def complete(self) -> "TurnContract":
        if self.state is not TurnState.STOPPING or self.owed_steps:
            raise ValueError("turn cannot complete before owed steps reconcile")
        return replace(self, state=TurnState.COMPLETED)

    def accept_steering(self) -> "TurnContract":
        if self.state in {TurnState.COMPLETED, TurnState.FAILED, TurnState.CANCELLED}:
            raise ValueError("cannot steer a terminal turn")
        return replace(self, accepted_steering=self.accepted_steering + 1)


@dataclass(frozen=True)
class StepContract:
    step_id: str
    turn_id: str
    state: StepState = StepState.CREATED
    attempt: int = 1

    def transition(self, state: StepState) -> "StepContract":
        state = StepState(state)
        if state not in _STEP_TRANSITIONS[self.state]:
            raise ValueError(f"invalid step transition {self.state.value}->{state.value}")
        return replace(self, state=state)

    def retry(self) -> "StepContract":
        if self.state not in {StepState.FAILED, StepState.MODEL_REQUESTED}:
            raise ValueError("only a failed/requested step may retry")
        return replace(self, state=StepState.RETRYING, attempt=self.attempt + 1)


@dataclass(frozen=True)
class InterceptionEvent:
    phase: InterceptionPhase
    turn_id: str
    step_id: str | None = None
    attempt: int = 0
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.attempt < 0:
            raise ValueError("attempt must be non-negative")
        if self.phase in {InterceptionPhase.ERROR, InterceptionPhase.RETRY} and not self.error_code:
            raise ValueError("error/retry phases require error_code")
