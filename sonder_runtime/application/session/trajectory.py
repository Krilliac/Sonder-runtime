"""Redacted action/observation trajectories projected from session events."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Iterable

from ...domain.common.errors import IntegrityFailure, InvalidInput
from ...domain.common.events import DomainEvent


MAX_TRAJECTORY_STEPS = 10_000


@dataclass(frozen=True, slots=True)
class TrajectoryStep:
    """A tool action and its observation without raw arguments or results."""

    session_id: str
    turn_id: str
    call_id: str
    tool: str
    status: str
    requested_sequence: int
    completed_sequence: int | None
    arguments_sha256: str
    result_sha256: str | None
    result_bytes: int | None
    failure_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "call_id": self.call_id,
            "tool": self.tool,
            "status": self.status,
            "requested_sequence": self.requested_sequence,
            "completed_sequence": self.completed_sequence,
            "arguments_sha256": self.arguments_sha256,
            "result_sha256": self.result_sha256,
            "result_bytes": self.result_bytes,
            "failure_code": self.failure_code,
            "redacted": True,
        }


@dataclass(frozen=True, slots=True)
class TrajectoryExport:
    session_id: str
    steps: tuple[TrajectoryStep, ...]
    integrity_valid: bool = True

    def to_jsonl(self) -> str:
        return "".join(
            json.dumps(step.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
            for step in self.steps
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "sonder.session-trajectory.v1",
            "session_id": self.session_id,
            "steps": [step.to_dict() for step in self.steps],
            "integrity_valid": self.integrity_valid,
        }


def project_trajectory(
    events: Iterable[DomainEvent], *, max_steps: int = MAX_TRAJECTORY_STEPS
) -> TrajectoryExport:
    """Build a bounded trajectory from authoritative, ordered session events.

    ``tool.call`` starts a step and ``tool.result``/``tool.failed`` closes it.
    Raw content is represented only by hashes and byte counts, making the
    projection suitable for inspection and evidence transport.
    """
    if not 1 <= max_steps <= MAX_TRAJECTORY_STEPS:
        raise InvalidInput("max_steps is out of bounds")
    ordered = tuple(events)
    if any(not isinstance(event, DomainEvent) for event in ordered):
        raise InvalidInput("trajectory requires DomainEvent records")
    ordered = tuple(sorted(ordered, key=lambda event: event.sequence))
    if not ordered:
        return TrajectoryExport("", ())
    session_id = ordered[0].aggregate_id
    if any(event.aggregate_id != session_id for event in ordered):
        raise IntegrityFailure("trajectory contains multiple session aggregates")
    active: dict[str, TrajectoryStep] = {}
    completed: list[TrajectoryStep] = []
    for event in ordered:
        payload = event.payload
        if event.event_type == "tool.call":
            call_id = _text(payload, "call_id")
            if call_id in active:
                raise IntegrityFailure("trajectory contains duplicate active tool calls")
            if len(active) + len(completed) >= max_steps:
                raise InvalidInput("trajectory exceeds max_steps")
            arguments = _text(payload, "content")
            active[call_id] = TrajectoryStep(
                session_id, _text(payload, "turn_id", default=""), call_id,
                _text(payload, "name"), "pending", event.sequence, None,
                _digest(arguments), None, None,
            )
        elif event.event_type in {"tool.result", "tool.failed"}:
            call_id = _text(payload, "call_id")
            try:
                current = active.pop(call_id)
            except KeyError as exc:
                raise IntegrityFailure("trajectory observation has no action") from exc
            if event.event_type == "tool.failed":
                completed.append(TrajectoryStep(
                    current.session_id, current.turn_id, current.call_id, current.tool,
                    "failed", current.requested_sequence, event.sequence,
                    current.arguments_sha256, None, None,
                    _text(payload, "error_code", default="TOOL_FAILED"),
                ))
                continue
            result = _text(payload, "content")
            completed.append(TrajectoryStep(
                current.session_id, current.turn_id, current.call_id, current.tool,
                "completed", current.requested_sequence, event.sequence,
                current.arguments_sha256, _digest(result), len(result.encode("utf-8")),
            ))
    completed.extend(active.values())
    return TrajectoryExport(session_id, tuple(sorted(completed, key=lambda step: step.requested_sequence)))


def _text(payload: dict, field: str, *, default: str | None = None) -> str:
    value = payload.get(field, default)
    if not isinstance(value, str):
        raise IntegrityFailure(f"trajectory field {field!r} is invalid")
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["TrajectoryExport", "TrajectoryStep", "project_trajectory", "MAX_TRAJECTORY_STEPS"]
