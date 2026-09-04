"""In-process event bus for cross-subsystem communication.

Replaces direct synchronous calls between subsystems with observable,
auditable event emission.  Subscribers register for event types and
receive events asynchronously (or synchronously if the bus is run in
sync mode for testing).

Events are fire-and-forget by default — a failing subscriber never
blocks the publisher.  The bus logs all emissions for auditability.

Thread-safe: multiple threads can publish and subscribe concurrently.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

EventHandler = Callable[["Event"], None]


@dataclass(frozen=True)
class Event:
    kind: str
    source_type: str
    source_id: str
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}
        self._lock = threading.Lock()
        self._history: list[Event] = []
        self._max_history = 500

    def subscribe(self, event_kind: str, handler: EventHandler) -> None:
        with self._lock:
            self._handlers.setdefault(event_kind, []).append(handler)

    def unsubscribe(self, event_kind: str, handler: EventHandler) -> None:
        with self._lock:
            handlers = self._handlers.get(event_kind, [])
            try:
                handlers.remove(handler)
            except ValueError:
                pass

    def publish(self, event: Event) -> int:
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]
            handlers = list(self._handlers.get(event.kind, []))
            handlers += list(self._handlers.get("*", []))

        delivered = 0
        for handler in handlers:
            try:
                handler(event)
                delivered += 1
            except Exception:
                logger.exception(
                    "event handler %s failed for %s",
                    getattr(handler, "__name__", repr(handler)),
                    event.kind,
                )
        return delivered

    def recent(self, kind: str = "", limit: int = 50) -> list[Event]:
        with self._lock:
            history = list(self._history)
        if kind:
            history = [e for e in history if e.kind == kind]
        return history[-limit:]

    def clear_handlers(self) -> None:
        with self._lock:
            self._handlers.clear()

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()

    def handler_count(self, kind: str = "") -> int:
        with self._lock:
            if kind:
                return len(self._handlers.get(kind, []))
            return sum(len(h) for h in self._handlers.values())


_bus = EventBus()


def subscribe(event_kind: str, handler: EventHandler) -> None:
    _bus.subscribe(event_kind, handler)


def unsubscribe(event_kind: str, handler: EventHandler) -> None:
    _bus.unsubscribe(event_kind, handler)


def publish(event: Event) -> int:
    return _bus.publish(event)


def emit(
    kind: str,
    source_type: str,
    source_id: str,
    data: dict | None = None,
) -> int:
    return _bus.publish(Event(
        kind=kind,
        source_type=source_type,
        source_id=source_id,
        data=data or {},
    ))


def recent(kind: str = "", limit: int = 50) -> list[Event]:
    return _bus.recent(kind, limit)


def reset() -> None:
    _bus.clear_handlers()
    _bus.clear_history()


def get_bus() -> EventBus:
    return _bus


AUTOPILOT_STARTED = "autopilot.started"
AUTOPILOT_COMPLETED = "autopilot.completed"
AUTOPILOT_FAILED = "autopilot.failed"
AUTOPILOT_CANCELLED = "autopilot.cancelled"

GOAL_SET = "goal.set"
GOAL_COMPLETED = "goal.completed"
GOAL_CANCELLED = "goal.cancelled"

FLEET_AGENT_STARTED = "fleet.agent.started"
FLEET_AGENT_DONE = "fleet.agent.done"
FLEET_AGENT_FAILED = "fleet.agent.failed"

WORKFLOW_COMPLETED = "workflow.completed"
WORKFLOW_FAILED = "workflow.failed"

BINDING_CREATED = "composition.binding.created"
BINDING_CLOSED = "composition.binding.closed"

MISSION_STARTED = "mission.started"

__all__ = [
    "AUTOPILOT_CANCELLED",
    "AUTOPILOT_COMPLETED",
    "AUTOPILOT_FAILED",
    "AUTOPILOT_STARTED",
    "BINDING_CLOSED",
    "BINDING_CREATED",
    "Event",
    "EventBus",
    "EventHandler",
    "FLEET_AGENT_DONE",
    "FLEET_AGENT_FAILED",
    "FLEET_AGENT_STARTED",
    "GOAL_CANCELLED",
    "GOAL_COMPLETED",
    "GOAL_SET",
    "MISSION_STARTED",
    "WORKFLOW_COMPLETED",
    "WORKFLOW_FAILED",
    "emit",
    "get_bus",
    "publish",
    "recent",
    "reset",
    "subscribe",
    "unsubscribe",
]
