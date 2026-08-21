"""Typed coordination contracts for domain-owned record/outbox writes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .outbox_cas import OutboxEvent, TransactionNeutralRecord


class CoordinationError(ValueError):
    """Base error for fail-closed coordination decisions."""


class CoordinationIdempotencyConflict(CoordinationError):
    """An idempotency key was reused for a different write set."""


class CoordinationRevisionConflict(CoordinationError):
    """A participant rejected its expected revision."""


@dataclass(frozen=True)
class CrossDomainWrite:
    """One domain-owned record/event pair in a coordinated write set."""

    domain: str
    record: TransactionNeutralRecord
    event: OutboxEvent
    expected_revision: int

    def __post_init__(self) -> None:
        if not self.domain.strip():
            raise CoordinationError("domain is required")
        if self.record.aggregate_id != self.event.aggregate_id:
            raise CoordinationError("record and event aggregate IDs must match")
        if self.record.revision != self.event.revision:
            raise CoordinationError("record and event revisions must match")
        if self.record.revision != self.expected_revision + 1:
            raise CoordinationError("record revision must advance expected_revision by one")


@dataclass(frozen=True)
class CoordinationResult:
    """Durable outcome; ``replayed`` means no second write occurred."""

    operation_id: str
    fingerprint: str
    committed: bool
    replayed: bool = False


class CrossDomainCoordinator(Protocol):
    """Application port for an atomic, idempotent cross-domain write."""

    def coordinate(
        self, operation_id: str, writes: tuple[CrossDomainWrite, ...]
    ) -> CoordinationResult: ...


__all__ = [
    "CoordinationError", "CoordinationIdempotencyConflict",
    "CoordinationRevisionConflict", "CoordinationResult", "CrossDomainCoordinator",
    "CrossDomainWrite",
]
