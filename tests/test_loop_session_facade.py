from __future__ import annotations

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.loop import LoopSessionLifecycleFacade
from sonder_runtime.application.loop_contract import InterceptionEvent, InterceptionPhase, StepState, TurnState
from sonder_runtime.application.loop_event_classification import DurableSessionFact, EphemeralLiveEvent
from sonder_runtime.application.loop_steering import SteeringCommand, SteeringKind
from sonder_runtime.application.ports.specialized_lifecycle import CleanupResult
from sonder_runtime.domain.common.errors import InvalidInput


class _LiveSink:
    def __init__(self) -> None:
        self.events = []

    def publish(self, event) -> None:
        self.events.append(event)


def test_facade_composes_turn_step_and_live_durable_boundaries(tmp_path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "session.db", max_read_limit=100)
    sink = _LiveSink()
    facade = LoopSessionLifecycleFacade(repository, live_sink=sink)

    facade.admit_turn("turn-1", session_id="session-1")
    facade.open_step("turn-1", "step-1")
    facade.intercept(InterceptionEvent(InterceptionPhase.PRE_STEP, "turn-1", "step-1"))
    facade.publish_live(EphemeralLiveEvent("capability.selected", {"name": "local"}))
    persisted = facade.record_fact(DurableSessionFact("model.requested", "session-1", {"turn_id": "turn-1"}))

    assert [event.phase.value if isinstance(event, InterceptionEvent) else event.event_type for event in sink.events] == ["pre_step", "capability.selected"]
    assert persisted.event_type == "model.requested"
    assert [event.event_type for event in repository.read_range("session-1", limit=10)] == ["model.requested"]

    facade.transition_step("turn-1", "step-1", StepState.EXECUTING)
    facade.complete_step("turn-1", "step-1")
    facade.complete_turn("turn-1")
    assert facade.snapshot("turn-1").turn.state is TurnState.COMPLETED


def test_facade_applies_control_steering_before_content_and_cancellation_is_truthful(tmp_path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "session.db")
    sink = _LiveSink()
    facade = LoopSessionLifecycleFacade(repository, live_sink=sink)
    facade.admit_turn("turn-1")

    facade.steer(SteeringCommand.follow_up("follow", 2, "later"), turn_id="turn-1")
    facade.steer(SteeringCommand.stop("stop", 1, "operator stop"), turn_id="turn-1")
    drained = facade.drain_steering("turn-1")

    assert [command.kind for command in drained] == [SteeringKind.STOP, SteeringKind.FOLLOW_UP]
    assert facade.snapshot("turn-1").turn.state is TurnState.STOPPING

    def cancel(reason: str) -> bool:
        return True

    def cleanup(timeout) -> CleanupResult:
        return CleanupResult("provider", True, True, "clean")

    facade.bind_cancellable("turn-1", "provider", cancel=cancel, cleanup=cleanup)
    reports = facade.cancel_turn("turn-1", reason="operator stop")
    assert reports[0].conforms
    assert facade.snapshot("turn-1").turn.state is TurnState.CANCELLED


def test_facade_rejects_unknown_turn_and_cannot_persist_live_event(tmp_path) -> None:
    facade = LoopSessionLifecycleFacade(SQLiteSessionRepository(tmp_path / "session.db"))
    with pytest.raises(InvalidInput, match="unknown turn_id"):
        facade.intercept(InterceptionEvent(InterceptionPhase.PRE_STEP, "missing"))
    with pytest.raises(TypeError):
        facade.record_fact(EphemeralLiveEvent("capability.selected"))  # type: ignore[arg-type]
