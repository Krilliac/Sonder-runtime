"""Pure reconstruction of model-visible session state from durable events."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from ...domain.common.errors import IntegrityFailure, InvalidInput
from ...domain.common.events import DomainEvent
from ..ports.model_gateway import ModelRequest
from .projections import SessionProjection, project_session


_MESSAGE_TYPES = {"user.message", "model.response", "tool.call", "tool.result"}


@dataclass(frozen=True)
class TranscriptMessage:
    role: str
    content: str
    event_type: str
    sequence: int
    turn_id: str = ""
    name: str = ""


@dataclass(frozen=True)
class SessionReplay:
    """All replay outputs, containing no live/session-owned mutable state."""

    session_id: str
    request: ModelRequest | None
    transcript: tuple[TranscriptMessage, ...]
    projection: SessionProjection
    event_count: int
    last_sequence: int


def _ordered(events: Iterable[DomainEvent]) -> tuple[DomainEvent, ...]:
    values = tuple(events)
    if not values:
        return ()
    if any(not isinstance(event, DomainEvent) for event in values):
        raise InvalidInput("replay requires DomainEvent records")
    values = tuple(sorted(values, key=lambda event: event.sequence))
    session_id = values[0].aggregate_id
    sequences = tuple(event.sequence for event in values)
    if any(event.aggregate_id != session_id for event in values):
        raise IntegrityFailure("session replay contains multiple aggregates")
    if len(set(sequences)) != len(sequences):
        raise IntegrityFailure("session replay contains duplicate sequence numbers")
    expected = tuple(range(1, len(values) + 1))
    if sequences != expected:
        raise IntegrityFailure("session replay has a gap or invalid starting sequence")
    return values


def _text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field, "")
    if not isinstance(value, str):
        raise IntegrityFailure(f"session event field {field!r} is not text")
    return value


def reconstruct_model_request(
    events: Iterable[DomainEvent], *, turn_id: str | None = None
) -> ModelRequest | None:
    """Return the durable request snapshot for a turn, if one exists.

    A request is accepted only from ``model.requested``/``prompt.snapshot``;
    silently rebuilding it from current configuration would violate SESSION-004.
    """
    request: ModelRequest | None = None
    for event in _ordered(events):
        if event.event_type not in {"model.requested", "prompt.snapshot"}:
            continue
        payload = event.payload
        if turn_id is not None and payload.get("turn_id") != turn_id:
            continue
        history = payload.get("history", ())
        options = payload.get("options", {})
        if not isinstance(history, (list, tuple)) or not isinstance(options, Mapping):
            raise IntegrityFailure("model request snapshot has invalid containers")
        request = ModelRequest(
            prompt=_text(payload, "prompt"),
            tier=_text(payload, "tier"),
            system=_text(payload, "system"),
            history=tuple(history),
            options=MappingProxyType(dict(options)),
            stream=payload.get("stream", False) is True,
        )
    return request


def reconstruct_transcript(events: Iterable[DomainEvent]) -> tuple[TranscriptMessage, ...]:
    """Rebuild the model/UI transcript using only durable message events."""
    result = []
    for event in _ordered(events):
        if event.event_type not in _MESSAGE_TYPES:
            continue
        payload = event.payload
        role = {"user.message": "user", "model.response": "assistant",
                "tool.call": "tool", "tool.result": "tool"}[event.event_type]
        result.append(TranscriptMessage(
            role=role,
            content=_text(payload, "content"),
            event_type=event.event_type,
            sequence=event.sequence,
            turn_id=payload.get("turn_id", "") if isinstance(payload.get("turn_id", ""), str) else "",
            name=payload.get("name", "") if isinstance(payload.get("name", ""), str) else "",
        ))
    return tuple(result)


def replay_session(events: Iterable[DomainEvent]) -> SessionReplay:
    """Reconstruct request, transcript, and projection deterministically."""
    ordered = _ordered(events)
    if not ordered:
        projection = project_session(())
        return SessionReplay("", None, (), projection, 0, 0)
    return SessionReplay(
        session_id=ordered[0].aggregate_id,
        request=reconstruct_model_request(ordered),
        transcript=reconstruct_transcript(ordered),
        projection=project_session(ordered),
        event_count=len(ordered),
        last_sequence=ordered[-1].sequence,
    )
