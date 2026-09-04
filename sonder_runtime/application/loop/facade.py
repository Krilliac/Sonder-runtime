"""Provider-neutral loop/session lifecycle composition.

This is the application boundary for a running turn.  It owns only the
in-memory lifecycle handles and delegates durable effects to the existing
session and loop ports.  Providers remain behind the injected live sink and
transport executors; no provider vocabulary crosses this boundary.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
import json
from threading import RLock
from typing import Any, Protocol

from ...domain.common.errors import InvalidInput

logger = logging.getLogger(__name__)
from ...domain.common.ids import SessionId
from ...domain.loop_retry_policy import RetryDecision
from ..loop_contract import (
    InterceptionEvent,
    StepContract,
    StepState,
    TurnContract,
    TurnState,
)
from ..loop_event_classification import (
    DurableSessionFact,
    EphemeralLiveEvent,
    classify_event,
)
from ..loop_steering import SteeringCommand, SteeringKind, order_commands
from ..ports.session_repository import SessionEvent, SessionRepository
from .durable_control import CleanupConformance, DurableLoopControl, IdempotencyStore
from .transport_retry import RetryExecutionResult, RetryTransport, TransportRetryExecutor
from ..session.continuity import RetentionExecution, SessionContinuityService
from ..session.checkpoint_privacy import RetentionCandidate
from ..session.checkpoints import ProjectionCheckpoint
from ..session.fork import ForkBoundary, SessionFork
from ..session.repair import SessionRepairPlan


MAX_FACT_BYTES = 2_000_000


class LiveLoopSink(Protocol):
    """Live-only delivery port; implementations must not persist events."""

    def publish(self, event: EphemeralLiveEvent | InterceptionEvent) -> None: ...


class NullLiveLoopSink:
    """Safe default for callers that do not need a live stream."""

    def publish(self, event: EphemeralLiveEvent | InterceptionEvent) -> None:
        return None


@dataclass(frozen=True, slots=True)
class LoopSnapshot:
    turn: TurnContract
    steps: tuple[StepContract, ...]
    steering: tuple[SteeringCommand, ...]


@dataclass
class _TurnState:
    session_id: str | None
    turn: TurnContract
    steps: dict[str, StepContract]
    steering: dict[str, SteeringCommand]
    cancellation_node: str


def _json_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    try:
        encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_FACT_BYTES:
            raise InvalidInput("loop fact exceeds the durable size bound")
        copied = json.loads(encoded)
    except InvalidInput:
        raise
    except (TypeError, ValueError) as exc:
        raise InvalidInput("loop fact must be JSON-serializable") from exc
    if not isinstance(copied, dict):  # pragma: no cover - json object is required above
        raise InvalidInput("loop fact must be a JSON object")
    return copied


class LoopSessionLifecycleFacade:
    """Compose typed loop control with canonical durable-session continuity.

    Public methods are application-thread safe.  Provider calls are not made
    here; a transport executor is supplied by the caller when a step needs
    provider I/O.  Unknown failures therefore remain the executor's
    fail-closed responsibility.
    """

    def __init__(
        self,
        repository: SessionRepository,
        *,
        continuity: SessionContinuityService | None = None,
        live_sink: LiveLoopSink | None = None,
        control: DurableLoopControl | None = None,
        max_turns: int = 256,
    ) -> None:
        if not callable(getattr(repository, "append", None)):
            raise TypeError("repository must provide the session repository port")
        if isinstance(max_turns, bool) or not 1 <= max_turns <= 10_000:
            raise ValueError("max_turns must be between 1 and 10000")
        self._repository = repository
        self._continuity = continuity or SessionContinuityService(repository)
        self._live_sink = live_sink or NullLiveLoopSink()
        if not callable(getattr(self._live_sink, "publish", None)):
            raise TypeError("live_sink must provide publish")
        self._control = control or DurableLoopControl()
        self._max_turns = max_turns
        self._turns: dict[str, _TurnState] = {}
        self._lock = RLock()

    def admit_turn(self, turn_id: str, *, session_id: str | SessionId | None = None) -> TurnContract:
        logger.debug(f"LoopSessionLifecycleFacade.admit_turn: turn_id={turn_id!r}, session_id={session_id!r}")
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise InvalidInput("turn_id must be non-empty")
        normalized_session = None if session_id is None else (
            session_id.serialize() if isinstance(session_id, SessionId) else session_id
        )
        if normalized_session is not None and (not isinstance(normalized_session, str) or not normalized_session.strip()):
            raise InvalidInput("session_id must be non-empty")
        with self._lock:
            if turn_id in self._turns:
                raise InvalidInput("turn_id is already admitted")
            if len(self._turns) >= self._max_turns:
                logger.warning(f"active turn bound exceeded: current={len(self._turns)}, max={self._max_turns}")
                raise InvalidInput("active turn bound exceeded")
            if len(self._turns) >= self._max_turns * 0.9:
                logger.warning(f"active turn count approaching limit: current={len(self._turns)}, max={self._max_turns}")
            node_id = f"turn:{turn_id}"
            self._control.cancellation.create_child(node_id=node_id)
            turn = TurnContract(turn_id)
            self._turns[turn_id] = _TurnState(normalized_session, turn, {}, {}, node_id)
            logger.info(f"turn admitted: turn_id={turn_id!r}, session_id={normalized_session!r}")
            return turn

    def start_turn(self, turn_id: str) -> TurnContract:
        logger.debug(f"LoopSessionLifecycleFacade.start_turn: turn_id={turn_id!r}")
        state = self._state(turn_id)
        with self._lock:
            state.turn = state.turn.transition(TurnState.RUNNING)
            return state.turn

    def open_step(self, turn_id: str, step_id: str) -> StepContract:
        logger.debug(f"LoopSessionLifecycleFacade.open_step: turn_id={turn_id!r}, step_id={step_id!r}")
        state = self._state(turn_id)
        if not isinstance(step_id, str) or not step_id.strip():
            raise InvalidInput("step_id must be non-empty")
        with self._lock:
            if step_id in state.steps:
                raise InvalidInput("step_id is already open")
            if state.turn.state not in {TurnState.ADMITTED, TurnState.RUNNING}:
                raise InvalidInput("cannot open a step on an inactive turn")
            if state.turn.state is TurnState.ADMITTED:
                state.turn = state.turn.transition(TurnState.RUNNING)
            state.turn = state.turn.owe_step()
            step = StepContract(step_id, turn_id).transition(StepState.MODEL_REQUESTED)
            state.steps[step_id] = step
            return step

    def transition_step(self, turn_id: str, step_id: str, next_state: StepState) -> StepContract:
        logger.debug(f"LoopSessionLifecycleFacade.transition_step: turn_id={turn_id!r}, step_id={step_id!r}, next_state={next_state.value!r}")
        state = self._state(turn_id)
        with self._lock:
            step = self._step(state, step_id)
            step = step.transition(next_state)
            state.steps[step_id] = step
            return step

    def retry_step(self, turn_id: str, step_id: str) -> StepContract:
        state = self._state(turn_id)
        with self._lock:
            step = self._step(state, step_id).retry()
            state.steps[step_id] = step
            return step

    def complete_step(self, turn_id: str, step_id: str) -> StepContract:
        logger.debug(f"LoopSessionLifecycleFacade.complete_step: turn_id={turn_id!r}, step_id={step_id!r}")
        state = self._state(turn_id)
        with self._lock:
            step = self._step(state, step_id)
            if step.state is not StepState.COMPLETED:
                step = step.transition(StepState.COMPLETED)
            state.steps[step_id] = step
            state.turn = state.turn.reconcile_step()
            return step

    def stop_turn(self, turn_id: str) -> TurnContract:
        logger.debug(f"LoopSessionLifecycleFacade.stop_turn: turn_id={turn_id!r}")
        state = self._state(turn_id)
        with self._lock:
            if state.turn.state is TurnState.STOPPING:
                return state.turn
            if state.turn.state is TurnState.ADMITTED:
                state.turn = state.turn.transition(TurnState.RUNNING)
            state.turn = state.turn.transition(TurnState.STOPPING)
            return state.turn

    def complete_turn(self, turn_id: str) -> TurnContract:
        logger.debug(f"LoopSessionLifecycleFacade.complete_turn: turn_id={turn_id!r}")
        state = self._state(turn_id)
        with self._lock:
            if state.turn.state is not TurnState.STOPPING:
                state.turn = state.turn.transition(TurnState.STOPPING)
            state.turn = state.turn.complete()
            logger.info(f"turn completed: turn_id={turn_id!r}")
            return state.turn

    def fail_turn(self, turn_id: str) -> TurnContract:
        logger.debug(f"LoopSessionLifecycleFacade.fail_turn: turn_id={turn_id!r}")
        state = self._state(turn_id)
        with self._lock:
            if state.turn.state in {TurnState.COMPLETED, TurnState.FAILED, TurnState.CANCELLED}:
                return state.turn
            state.turn = state.turn.transition(TurnState.FAILED)
            logger.error(f"turn failed: turn_id={turn_id!r}")
            logger.warning(f"turn failed: turn_id={turn_id!r}")
            logger.info(f"turn failed: turn_id={turn_id!r}")
            return state.turn

    def cancel_turn(self, turn_id: str, *, reason: str = "cancellation requested", timeout: float | None = None) -> tuple[CleanupConformance, ...]:
        logger.debug(f"LoopSessionLifecycleFacade.cancel_turn: turn_id={turn_id!r}, reason={reason!r}")
        state = self._state(turn_id)
        reports = self._control.cancel_and_cleanup(state.cancellation_node, reason=reason, timeout=timeout)
        with self._lock:
            if state.turn.state not in {TurnState.COMPLETED, TurnState.FAILED, TurnState.CANCELLED}:
                state.turn = state.turn.transition(TurnState.CANCELLED)
            logger.info(f"turn cancelled: turn_id={turn_id!r}, reason={reason!r}, cleanup_reports={len(reports)}")
            return reports

    def bind_cancellable(self, turn_id: str, target_id: str, *, cancel, cleanup) -> None:
        state = self._state(turn_id)
        self._control.bind(state.cancellation_node, target_id, cancel=cancel, cleanup=cleanup)

    def intercept(self, event: InterceptionEvent) -> None:
        if not isinstance(event, InterceptionEvent):
            raise TypeError("event must be an InterceptionEvent")
        self._state(event.turn_id)
        self._live_sink.publish(event)

    def publish_live(self, event: EphemeralLiveEvent) -> None:
        if not isinstance(event, EphemeralLiveEvent):
            raise TypeError("event must be an EphemeralLiveEvent")
        self._live_sink.publish(event)

    def record_fact(self, fact: DurableSessionFact) -> SessionEvent:
        if not isinstance(fact, DurableSessionFact):
            raise TypeError("fact must be a DurableSessionFact")
        classify_event(fact.event_type)
        return self._repository.append(fact.session_id, fact.event_type, _json_payload(fact.payload))

    def steer(self, command: SteeringCommand, *, turn_id: str) -> SteeringCommand:
        logger.debug(f"LoopSessionLifecycleFacade.steer: turn_id={turn_id!r}, command_id={command.command_id!r}, kind={command.kind.value!r}")
        state = self._state(turn_id)
        if not isinstance(command, SteeringCommand):
            raise TypeError("command must be a SteeringCommand")
        with self._lock:
            if command.command_id in state.steering:
                raise InvalidInput("steering command is already admitted")
            state.turn = state.turn.accept_steering()
            state.steering[command.command_id] = command
            return command

    def drain_steering(self, turn_id: str) -> tuple[SteeringCommand, ...]:
        logger.debug(f"LoopSessionLifecycleFacade.drain_steering: turn_id={turn_id!r}")
        state = self._state(turn_id)
        with self._lock:
            commands = order_commands(state.steering.values())
            state.steering.clear()
        if commands:
            logger.warning(f"draining {len(commands)} steering command(s) for turn_id={turn_id!r}")
        for command in commands:
            if command.kind is SteeringKind.CANCELLATION:
                self.cancel_turn(turn_id, reason=command.reason)
            elif command.kind is SteeringKind.STOP:
                self.stop_turn(turn_id)
        return commands

    def snapshot(self, turn_id: str) -> LoopSnapshot:
        state = self._state(turn_id)
        with self._lock:
            return LoopSnapshot(state.turn, tuple(state.steps.values()), tuple(order_commands(state.steering.values())))

    def retry_decision(self, operation_id: str, **kwargs: Any) -> RetryDecision:
        return self._control.retry(operation_id, **kwargs)

    def transport_executor(
        self,
        turn_id: str,
        transport: RetryTransport[Any, Any],
        *,
        idempotency: IdempotencyStore,
        sleep=None,
        max_sleep_seconds: float = 30.0,
    ) -> TransportRetryExecutor[Any, Any]:
        """Build the bounded retry boundary fenced to one turn's cancellation node."""
        state = self._state(turn_id)
        kwargs: dict[str, Any] = {"max_sleep_seconds": max_sleep_seconds}
        if sleep is not None:
            kwargs["sleep"] = sleep
        return TransportRetryExecutor(
            transport,
            idempotency=idempotency,
            evidence=self._control.ledger,
            cancellation=self._control.cancellation.node(state.cancellation_node),
            **kwargs,
        )

    @staticmethod
    def execute_retry(
        executor: TransportRetryExecutor[Any, Any], operation_id: str, request: Any, **kwargs: Any
    ) -> RetryExecutionResult[Any]:
        if not isinstance(executor, TransportRetryExecutor):
            raise TypeError("executor must be a TransportRetryExecutor")
        return executor.execute(operation_id, request, **kwargs)

    # SESSION-006/007/009/010 composition remains delegated to the canonical service.
    def fork_session(self, session_id: str, boundary: ForkBoundary | int, *, child_session_id: SessionId | None = None) -> SessionFork:
        return self._continuity.fork(session_id, boundary, child_session_id=child_session_id)

    def resume_session(self, session_id: str) -> SessionRepairPlan:
        return self._continuity.resume(session_id)

    def checkpoint_session(self, session_id: str, *, projection_version: int = 1, context=None) -> ProjectionCheckpoint:
        return self._continuity.checkpoint(session_id, projection_version=projection_version, context=context)

    def load_checkpoint(self, session_id: str) -> ProjectionCheckpoint | None:
        return self._continuity.load_checkpoint(session_id)

    def retention_candidates(self, session_id: str, **kwargs: Any) -> tuple[RetentionCandidate, ...]:
        return self._continuity.retention_candidates(session_id, **kwargs)

    def execute_retention(self, session_id: str, *, context=None, **kwargs: Any) -> RetentionExecution:
        return self._continuity.execute_retention(session_id, context=context, **kwargs)

    def _state(self, turn_id: str) -> _TurnState:
        with self._lock:
            try:
                return self._turns[turn_id]
            except KeyError as exc:
                raise InvalidInput("unknown turn_id") from exc

    @staticmethod
    def _step(state: _TurnState, step_id: str) -> StepContract:
        try:
            return state.steps[step_id]
        except KeyError as exc:
            raise InvalidInput("unknown step_id") from exc


__all__ = ["LiveLoopSink", "LoopSessionLifecycleFacade", "LoopSnapshot", "NullLiveLoopSink"]
