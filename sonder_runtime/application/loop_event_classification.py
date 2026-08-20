"""Classify application-loop events by retention and delivery semantics.

This is an application boundary, not an event store or dispatcher.  The
vocabularies are intentionally explicit so an adapter cannot accidentally
turn a live control-plane observation into durable session history.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class UnknownLoopEventError(ValueError):
    """Raised when an event has no declared loop retention classification."""


class LoopEventClass(str, Enum):
    DURABLE_SESSION_FACT = "durable_session_fact"
    EPHEMERAL_INTERCEPTION = "ephemeral_interception"
    EPHEMERAL_CAPABILITY = "ephemeral_capability"


class EventRetention(str, Enum):
    DURABLE = "durable"
    EPHEMERAL = "ephemeral"


_DURABLE_SESSION_FACTS = frozenset({
    "session.started", "session.ended", "session.paused", "session.resumed",
    "message.received", "message.emitted", "message.completed",
    "context.updated", "context.window_changed", "prompt.created",
    "prompt.submitted", "model.requested", "model.started",
    "model.completed", "model.failed", "tool.requested", "tool.started",
    "tool.completed", "tool.failed", "approval.requested", "approval.granted",
    "approval.denied", "goal.created", "goal.updated", "goal.completed",
    "plan.created", "plan.step_started", "plan.step_completed",
    "compaction.started", "compaction.completed", "retrieval.requested",
    "retrieval.completed", "subagent.spawned", "subagent.started",
    "subagent.completed", "subagent.failed", "cancellation.requested",
    "cancellation.completed", "error.raised", "error.recovered",
    "artifact.created", "artifact.updated", "artifact.attached",
})

_INTERCEPTION_EVENTS = frozenset({
    "pre_step", "model_request", "pre_execute", "execute", "post_execute",
    "turn_stopping", "error", "retry",
})

_CAPABILITY_EVENTS = frozenset({
    "capability.check", "capability.available", "capability.unavailable",
    "capability.selected", "capability.denied", "capability.granted",
    "capability.revoked",
})


@dataclass(frozen=True)
class LoopEventClassification:
    """The retention and live-channel meaning of one known event type."""

    event_type: str
    event_class: LoopEventClass

    @property
    def retention(self) -> EventRetention:
        return (EventRetention.DURABLE
                if self.event_class is LoopEventClass.DURABLE_SESSION_FACT
                else EventRetention.EPHEMERAL)

    @property
    def is_durable(self) -> bool:
        return self.retention is EventRetention.DURABLE

    @property
    def is_ephemeral(self) -> bool:
        return not self.is_durable


def classify_event(event_type: str) -> LoopEventClassification:
    """Return the explicit classification for ``event_type``.

    Unknown types fail closed.  Callers must register a new vocabulary item
    before it can be persisted or emitted as a recognized live event.
    """
    if not isinstance(event_type, str) or not event_type.strip():
        raise UnknownLoopEventError("event_type must be a non-empty string")
    normalized = event_type.strip()
    if normalized in _DURABLE_SESSION_FACTS:
        event_class = LoopEventClass.DURABLE_SESSION_FACT
    elif normalized in _INTERCEPTION_EVENTS:
        event_class = LoopEventClass.EPHEMERAL_INTERCEPTION
    elif normalized in _CAPABILITY_EVENTS:
        event_class = LoopEventClass.EPHEMERAL_CAPABILITY
    else:
        raise UnknownLoopEventError(f"unknown loop event type: {event_type!r}")
    return LoopEventClassification(normalized, event_class)


def is_durable_session_fact(event_type: str) -> bool:
    """Whether ``event_type`` is a known durable session fact."""
    return classify_event(event_type).event_class is LoopEventClass.DURABLE_SESSION_FACT


def is_ephemeral_live_event(event_type: str) -> bool:
    """Whether ``event_type`` is a known interception or capability event."""
    return classify_event(event_type).is_ephemeral


@dataclass(frozen=True)
class DurableSessionFact:
    """Immutable application data eligible for session persistence."""

    event_type: str
    session_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        classification = classify_event(self.event_type)
        if classification.event_class is not LoopEventClass.DURABLE_SESSION_FACT:
            raise ValueError("only durable session facts may use DurableSessionFact")
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        object.__setattr__(self, "event_type", classification.event_type)
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class EphemeralLiveEvent:
    """Immutable live control-plane data; never a session-history fact."""

    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        classification = classify_event(self.event_type)
        if not classification.is_ephemeral:
            raise ValueError("only ephemeral events may use EphemeralLiveEvent")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        object.__setattr__(self, "event_type", classification.event_type)
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


__all__ = [
    "DurableSessionFact", "EphemeralLiveEvent", "EventRetention",
    "LoopEventClass", "LoopEventClassification", "UnknownLoopEventError",
    "classify_event", "is_durable_session_fact", "is_ephemeral_live_event",
]
