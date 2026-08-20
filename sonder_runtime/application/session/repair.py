"""Read-only diagnosis and planning for damaged session tails (WP2 SESSION-007)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ...domain.common.events import DomainEvent


@dataclass(frozen=True, slots=True)
class RepairIssue:
    sequence: int | None
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class SessionTailDiagnosis:
    """Evidence about the maximal prefix safe to use for a new attempt."""

    session_id: str
    disposition: str
    valid_boundary: int
    resume_sequence: int
    checked_events: int
    issues: tuple[RepairIssue, ...]

    @property
    def can_resume(self) -> bool:
        return self.disposition != "inconsistent"


@dataclass(frozen=True, slots=True)
class SessionRepairPlan:
    """An immutable plan; no repository write or effect replay is performed."""

    diagnosis: SessionTailDiagnosis
    safe_prefix: tuple[DomainEvent, ...]
    discarded_tail: tuple[DomainEvent, ...]

    @property
    def resume_sequence(self) -> int:
        return self.diagnosis.resume_sequence

    @property
    def valid_boundary(self) -> int:
        return self.diagnosis.valid_boundary


_IN_FLIGHT = {
    "model.requested": "model completion or failure",
    "model.started": "model completion or failure",
    "tool.requested": "tool completion or failure",
    "tool.started": "tool completion or failure",
    "approval.requested": "approval grant or denial",
    "compaction.started": "compaction completion",
    "retrieval.requested": "retrieval completion",
    "subagent.spawned": "subagent completion or failure",
    "subagent.started": "subagent completion or failure",
}
_TERMINALS = {
    "model.completed", "model.failed", "tool.completed", "tool.failed",
    "approval.granted", "approval.denied", "compaction.completed",
    "retrieval.completed", "subagent.completed", "subagent.failed",
}


def _operation_key(event: DomainEvent) -> tuple[str, str]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    field = {
        "model": "request_id", "tool": "call_id", "approval": "approval_id",
        "compaction": "compaction_id", "retrieval": "retrieval_id",
        "subagent": "subagent_id",
    }.get(event.event_type.split(".", 1)[0], "")
    value = payload.get(field) if field else None
    return event.event_type.split(".", 1)[0], value if isinstance(value, str) else event.id


def _ordered_prefix(events: tuple[object, ...]) -> tuple[str, tuple[DomainEvent, ...], list[RepairIssue]]:
    issues: list[RepairIssue] = []
    if any(not isinstance(event, DomainEvent) for event in events):
        return "", (), [RepairIssue(None, "invalid_event", "repair requires DomainEvent records")]
    if not events:
        return "", (), issues
    ordered = sorted(events, key=lambda event: event.sequence)
    session_id = ordered[0].aggregate_id
    accepted: list[DomainEvent] = []
    expected = 1
    for event in ordered:
        if not isinstance(event.sequence, int) or isinstance(event.sequence, bool) or event.sequence < 1:
            issues.append(RepairIssue(event.sequence if isinstance(event.sequence, int) else None, "invalid_sequence", "sequence must be positive"))
            break
        if event.aggregate_id != session_id:
            issues.append(RepairIssue(event.sequence, "cross_session", "event belongs to another session"))
            break
        if event.sequence != expected:
            code = "duplicate_sequence" if event.sequence < expected else "sequence_gap"
            issues.append(RepairIssue(event.sequence, code, f"expected sequence {expected}"))
            break
        accepted.append(event)
        expected += 1
    return session_id, tuple(accepted), issues


def diagnose_session_tail(events: Iterable[DomainEvent]) -> SessionTailDiagnosis:
    """Find the safe boundary without replaying or modifying any side effect."""
    raw = tuple(events)
    session_id, accepted, issues = _ordered_prefix(raw)
    boundary = len(accepted)
    pending: dict[tuple[str, str], int] = {}
    for index, event in enumerate(accepted):
        if event.event_type in _IN_FLIGHT:
            pending.setdefault(_operation_key(event), index)
        elif event.event_type in _TERMINALS:
            pending.pop(_operation_key(event), None)
    if not issues and pending:
        first = min(pending.values())
        boundary = first
        event = accepted[first]
        issues.append(RepairIssue(event.sequence, "truncated_tail", f"missing {_IN_FLIGHT[event.event_type]}"))
    disposition = "inconsistent" if any(issue.code != "truncated_tail" for issue in issues) else ("truncated" if issues else "clean")
    return SessionTailDiagnosis(session_id, disposition, boundary, boundary + 1, len(raw), tuple(issues))


def plan_session_resume(events: Iterable[DomainEvent]) -> SessionRepairPlan:
    """Return the safe prefix and fresh sequence for an explicit resume."""
    raw = tuple(events)
    diagnosis = diagnose_session_tail(raw)
    ordered = tuple(sorted((event for event in raw if isinstance(event, DomainEvent)), key=lambda event: event.sequence))
    boundary = diagnosis.valid_boundary
    return SessionRepairPlan(diagnosis, ordered[:boundary], ordered[boundary:])


__all__ = ["RepairIssue", "SessionTailDiagnosis", "SessionRepairPlan", "diagnose_session_tail", "plan_session_resume"]
