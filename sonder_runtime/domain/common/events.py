"""Domain event base types (SPEC-5 §9).

Events are the cross-domain communication primitive.  Every state-owning
domain emits events atomically with its state mutations (transactional
outbox).  The LocalEventDispatcher copies them to operations.db.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import uuid


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class DomainEvent:
    event_type: str
    aggregate_type: str
    aggregate_id: str
    sequence: int
    payload: dict = field(default_factory=dict)
    correlation_id: str = ""
    id: str = field(default_factory=_new_id)
    created_at: str = field(default_factory=_utcnow)
