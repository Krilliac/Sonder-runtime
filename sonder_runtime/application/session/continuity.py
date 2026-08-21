"""Bounded durable-session continuity orchestration.

The pure fork/repair/checkpoint contracts remain the authorities for their
individual decisions.  This service only loads a bounded, integrity-checked
repository snapshot and connects those contracts to the canonical session
repository.  Retention execution is represented by append-only privacy
markers; the source history is never updated or deleted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from ...domain.common.errors import IntegrityFailure, InvalidInput
from ...domain.common.events import DomainEvent
from ...domain.common.ids import SessionId
from ...domain.session_privacy import EventPrivacyClass
from ..context import LOCAL_OWNER, OperationContext
from ..ports.session_repository import SessionRepository, SessionEvent
from .checkpoint_privacy import CHECKPOINT_EVENT, RetentionCandidate, SessionCheckpointPrivacyService
from .checkpoints import ProjectionCheckpoint, checkpoint_projection
from .fork import ForkBoundary, SessionFork, fork_session
from .repair import SessionRepairPlan, plan_session_resume


@dataclass(frozen=True, slots=True)
class RetentionExecution:
    session_id: str
    applied: tuple[RetentionCandidate, ...]
    marker: SessionEvent | None


def _domain_events(events: Iterable[SessionEvent]) -> tuple[DomainEvent, ...]:
    return tuple(
        DomainEvent(
            event_type=event.event_type,
            aggregate_type="session",
            aggregate_id=event.session_id,
            sequence=event.sequence,
            payload=dict(event.payload),
            id=event.event_id,
            created_at=event.occurred_at_utc,
        )
        for event in events
    )


class SessionContinuityService:
    """Canonical, bounded orchestration for production session continuity."""

    RETENTION_MARKER_EVENT = "session.retention.applied"

    def __init__(
        self,
        repository: SessionRepository,
        checkpoint_privacy: SessionCheckpointPrivacyService | None = None,
        *,
        max_events: int = 10_000,
    ) -> None:
        if isinstance(max_events, bool) or not isinstance(max_events, int) or not 1 <= max_events <= 100_000:
            raise ValueError("max_events must be between 1 and 100000")
        self._repository = repository
        self._checkpoint_privacy = checkpoint_privacy or SessionCheckpointPrivacyService(repository)
        self._max_events = max_events

    def _snapshot(self, session_id: str) -> tuple[SessionEvent, ...]:
        if not isinstance(session_id, str) or not session_id.strip():
            raise InvalidInput("session_id must be non-empty")
        adapter_limit = getattr(self._repository, "_max_read_limit", self._max_events)
        if isinstance(adapter_limit, bool) or not isinstance(adapter_limit, int) or adapter_limit < 1:
            adapter_limit = self._max_events
        limit = min(self._max_events, adapter_limit)
        events = self._repository.read_range(session_id, limit=limit)
        report = self._repository.inspect_integrity(session_id, limit=limit)
        if not report.valid or report.checked_events != len(events):
            raise IntegrityFailure("session history failed integrity verification")
        if (
            report.first_sequence != (events[0].sequence if events else None)
            or report.last_sequence != (events[-1].sequence if events else None)
        ):
            raise IntegrityFailure("session integrity report does not match continuity snapshot")
        if len(events) == limit:
            raise IntegrityFailure("session history exceeds continuity bound")
        return events

    def fork(
        self,
        session_id: str,
        boundary: ForkBoundary | int,
        *,
        child_session_id: SessionId | None = None,
    ) -> SessionFork:
        """Plan a fork from an integrity-checked durable source stream."""
        events = _domain_events(self._snapshot(session_id))
        return fork_session(events, boundary, child_session_id=child_session_id)

    def resume(self, session_id: str) -> SessionRepairPlan:
        """Plan a safe resume without replaying discarded effectful tails."""
        return plan_session_resume(_domain_events(self._snapshot(session_id)))

    def checkpoint(self, session_id: str, *, projection_version: int = 1,
                   context: OperationContext | None = None) -> ProjectionCheckpoint:
        """Build and durably save a source-pinned projection checkpoint."""
        if context is None or context.principal_id != LOCAL_OWNER or context.expired:
            raise PermissionError("local owner authority is required for checkpoint creation")
        events = tuple(event for event in self._snapshot(session_id)
                       if event.event_type not in {CHECKPOINT_EVENT, self.RETENTION_MARKER_EVENT})
        if not events:
            raise InvalidInput("cannot checkpoint an empty session")
        checkpoint = checkpoint_projection(
            _domain_events(events),
            source_hash=events[-1].event_hash,
            projection_version=projection_version,
        )
        self._checkpoint_privacy.save_checkpoint(checkpoint)
        return checkpoint

    def load_checkpoint(self, session_id: str) -> ProjectionCheckpoint | None:
        """Load only a checkpoint that is current for the verified stream."""
        events = tuple(event for event in self._snapshot(session_id)
                       if event.event_type not in {CHECKPOINT_EVENT, self.RETENTION_MARKER_EVENT})
        if not events:
            return None
        return self._checkpoint_privacy.load_checkpoint(
            session_id,
            source_sequence=events[-1].sequence,
            source_hash=events[-1].event_hash,
        )

    def retention_candidates(
        self, session_id: str, *, now_utc: datetime | None = None, limit: int | None = None,
    ) -> tuple[RetentionCandidate, ...]:
        self._snapshot(session_id)
        return self._checkpoint_privacy.retention_candidates(session_id, now_utc=now_utc, limit=limit)

    def execute_retention(
        self, session_id: str, *, now_utc: datetime | None = None, limit: int | None = None,
        context: OperationContext | None = None,
    ) -> RetentionExecution:
        """Append bounded privacy markers; never mutate or delete history."""
        if context is None or context.principal_id != LOCAL_OWNER or context.expired:
            raise PermissionError("local owner authority is required for retention execution")
        candidates = self.retention_candidates(session_id, now_utc=now_utc, limit=limit)
        marker_limit = min(self._max_events, getattr(self._repository, "_max_read_limit", self._max_events))
        applied_ids: set[tuple[object, object]] = set()
        for event in self._repository.search(
            session_id=session_id, event_type=self.RETENTION_MARKER_EVENT, limit=marker_limit
        ):
            targets = event.payload.get("targets")
            if not isinstance(targets, list):
                raise IntegrityFailure("retention marker envelope is invalid")
            for target in targets:
                if not isinstance(target, dict):
                    raise IntegrityFailure("retention marker target is invalid")
                applied_ids.add((target.get("event_id"), target.get("sequence")))
        applied = tuple(
            candidate for candidate in candidates
            if (candidate.event_id, candidate.sequence) not in applied_ids
        )
        if not applied:
            return RetentionExecution(session_id, (), None)
        marker = self._repository.append(
            session_id,
            self.RETENTION_MARKER_EVENT,
            {
                "privacy_class": EventPrivacyClass.PUBLIC_METADATA.value,
                "action": "redact_or_expire",
                "targets": [
                    {"event_id": item.event_id, "sequence": item.sequence,
                     "privacy_class": item.privacy_class.value,
                     "expires_at_utc": item.expires_at_utc}
                    for item in applied
                ],
            },
        )
        return RetentionExecution(session_id, applied, marker)


__all__ = ["RetentionExecution", "SessionContinuityService"]
