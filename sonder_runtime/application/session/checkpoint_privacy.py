"""Canonical-session integration for projection checkpoints and retention."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from collections.abc import Mapping

from ...domain.common.errors import IntegrityFailure, InvalidInput
from ...domain.session_privacy import EventPrivacyClass, rule_for
from ..ports.session_repository import SessionEvent, SessionRepository
from .checkpoints import ProjectionCheckpoint
from .projections import SessionProjection

CHECKPOINT_EVENT = "session.projection_checkpoint"
MAX_CHECKPOINT_BYTES = 64 * 1024
MAX_RETENTION_SCAN = 10_000

@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    session_id: str
    sequence: int
    event_id: str
    privacy_class: EventPrivacyClass
    expires_at_utc: str

def _projection_payload(projection: SessionProjection | Mapping[str, object]) -> dict[str, object]:
    if isinstance(projection, SessionProjection):
        return {name: getattr(projection, name) for name in projection.__dataclass_fields__}
    return dict(projection)

def _projection(value: object) -> SessionProjection:
    if not isinstance(value, Mapping):
        raise IntegrityFailure("checkpoint projection is not an object")
    required = set(SessionProjection.__dataclass_fields__)
    if set(value) != required:
        raise IntegrityFailure("checkpoint projection fields are invalid")
    try:
        return SessionProjection(**{name: value[name] for name in required})
    except (TypeError, ValueError) as exc:
        raise IntegrityFailure("checkpoint projection values are invalid") from exc

def _checkpoint_payload(checkpoint: ProjectionCheckpoint) -> dict[str, object]:
    payload = {
        "checkpoint_digest": checkpoint.digest(),
        "projection_version": checkpoint.projection_version,
        "source_sequence": checkpoint.source_sequence,
        "source_hash": checkpoint.source_hash,
        "projection": _projection_payload(checkpoint.projection),
        "privacy_class": EventPrivacyClass.PUBLIC_METADATA.value,
    }
    if len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()) > MAX_CHECKPOINT_BYTES:
        raise InvalidInput("checkpoint exceeds durable size bound")
    return payload

class SessionCheckpointPrivacyService:
    """Persist and validate session checkpoints and retention decisions."""

    def __init__(self, repository: SessionRepository, *, max_scan: int = MAX_RETENTION_SCAN) -> None:
        if isinstance(max_scan, bool) or not isinstance(max_scan, int) or not 1 <= max_scan <= MAX_RETENTION_SCAN:
            raise ValueError(f"max_scan must be between 1 and {MAX_RETENTION_SCAN}")
        self._repository = repository
        self._max_scan = max_scan

    def save_checkpoint(self, checkpoint: ProjectionCheckpoint) -> SessionEvent:
        if not isinstance(checkpoint, ProjectionCheckpoint):
            raise InvalidInput("checkpoint must be a ProjectionCheckpoint")
        payload = _checkpoint_payload(checkpoint)
        search_limit = min(self._max_scan, getattr(self._repository, "_max_read_limit", self._max_scan))
        existing = self._repository.search(session_id=checkpoint.session_id, event_type=CHECKPOINT_EVENT, limit=search_limit)
        for event in existing:
            if event.payload.get("checkpoint_digest") == payload["checkpoint_digest"]:
                return event
        return self._repository.append(checkpoint.session_id, CHECKPOINT_EVENT, payload)

    def load_checkpoint(self, session_id: str, *, source_sequence: int | None = None,
                        source_hash: str | None = None) -> ProjectionCheckpoint | None:
        search_limit = min(self._max_scan, getattr(self._repository, "_max_read_limit", self._max_scan))
        events = self._repository.search(session_id=session_id, event_type=CHECKPOINT_EVENT, limit=search_limit)
        if not events:
            return None
        event = events[-1]
        payload = event.payload
        try:
            checkpoint = ProjectionCheckpoint.create(
                session_id=event.session_id, projection_version=payload["projection_version"],
                source_sequence=payload["source_sequence"], source_hash=payload["source_hash"],
                projection=_projection(payload["projection"]),
            )
            if payload.get("checkpoint_digest") != checkpoint.digest():
                raise IntegrityFailure("checkpoint digest mismatch")
            if source_sequence is not None or source_hash is not None:
                if source_sequence is None or source_hash is None:
                    raise InvalidInput("source_sequence and source_hash must be supplied together")
                checkpoint.require_fresh(source_sequence, source_hash)
            return checkpoint
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityFailure("durable checkpoint envelope is invalid") from exc

    def retention_candidates(self, session_id: str, *, now_utc: datetime | None = None,
                             limit: int | None = None) -> tuple[RetentionCandidate, ...]:
        scan_limit = self._max_scan if limit is None else limit
        if isinstance(scan_limit, bool) or not isinstance(scan_limit, int) or not 1 <= scan_limit <= self._max_scan:
            raise InvalidInput(f"limit must be between 1 and {self._max_scan}")
        adapter_limit = getattr(self._repository, "_max_read_limit", scan_limit)
        if isinstance(adapter_limit, bool) or not isinstance(adapter_limit, int) or adapter_limit < 1:
            adapter_limit = scan_limit
        scan_limit = min(scan_limit, adapter_limit)
        now = now_utc or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise InvalidInput("now_utc must be timezone-aware")
        result: list[RetentionCandidate] = []
        for event in self._repository.read_range(session_id, limit=scan_limit):
            try:
                privacy_class = EventPrivacyClass(event.payload.get("privacy_class", EventPrivacyClass.PUBLIC_METADATA.value))
                rule = rule_for(privacy_class)
                occurred = datetime.fromisoformat(event.occurred_at_utc.replace("Z", "+00:00"))
                if occurred.tzinfo is None or rule.retention_days is None or not rule.allow_delete:
                    continue
            except (TypeError, ValueError):
                continue
            expires = occurred + timedelta(days=rule.retention_days)
            if expires <= now:
                result.append(RetentionCandidate(event.session_id, event.sequence, event.event_id,
                    privacy_class, expires.isoformat().replace("+00:00", "Z")))
        return tuple(result)

__all__ = ["CHECKPOINT_EVENT", "MAX_CHECKPOINT_BYTES", "RetentionCandidate", "SessionCheckpointPrivacyService"]
