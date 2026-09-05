"""Deterministic, read-only session projections.

The event stream is the only input.  This module deliberately has no cache,
clock, environment access, or adapter dependency.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from ...domain.common.events import DomainEvent


@dataclass(frozen=True)
class SessionProjection:
    """Operational facts that can be rebuilt from a session event stream."""

    session_id: str
    status: str
    turn_count: int
    user_message_count: int
    assistant_message_count: int
    tool_call_count: int
    tool_result_count: int
    error_count: int
    event_count: int
    last_sequence: int


def project_session(events: Iterable[DomainEvent]) -> SessionProjection:
    """Build the bounded operational projection from durable events.

    ``events`` must already be the stream for one session.  Replay validation
    (including ordering and identity) is shared with :func:`replay_session`;
    this function remains useful on its own for callers that only need the
    operational view.
    """
    ordered = tuple(events)
    if not ordered:
        return SessionProjection("", "empty", 0, 0, 0, 0, 0, 0, 0, 0)

    session_id = ordered[0].aggregate_id
    status = "active"
    turns: set[str] = set()
    counts = {
        "user.message": 0,
        "model.response": 0,
        "tool.call": 0,
        "tool.result": 0,
        "error": 0,
        "model.failed": 0,
    }
    for event in ordered:
        if event.aggregate_id != session_id:
            raise ValueError("session projection requires one aggregate")
        kind = event.event_type
        if kind in counts:
            counts[kind] += 1
        turn_id = event.payload.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            turns.add(turn_id)
        if kind in {"session.closed", "session.completed"}:
            status = "closed"
        elif kind in {"session.cancelled", "turn.cancelled"}:
            status = "cancelled"
        elif kind == "session.failed":
            status = "failed"

    return SessionProjection(
        session_id=session_id,
        status=status,
        turn_count=len(turns),
        user_message_count=counts["user.message"],
        assistant_message_count=counts["model.response"],
        tool_call_count=counts["tool.call"],
        tool_result_count=counts["tool.result"],
        error_count=counts["error"] + counts["model.failed"],
        event_count=len(ordered),
        last_sequence=ordered[-1].sequence,
    )
