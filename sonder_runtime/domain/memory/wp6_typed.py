"""Typed memory records for evidence-backed procedural learning.

This module is deliberately pure: it models memory and evidence but performs
no persistence, embedding, or model calls.  The application layer decides
which records are eligible for promotion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from math import isfinite


class MemoryLabel(str, Enum):
    PROCEDURAL = "procedural"
    FACTUAL = "factual"
    EPISODIC = "episodic"
    PREFERENCE = "preference"


class EvidenceKind(str, Enum):
    TEST_PASS = "tests_passed"
    COMPILED = "compiled"
    USED = "used"
    ACCEPTED = "accepted"
    EXPLICIT = "explicit"
    CONTRADICTION = "contradiction"


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Evidence:
    """A bounded, attributable observation about a memory."""

    kind: EvidenceKind
    source: str
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    weight: float = 1.0
    reference: str | None = None

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("evidence source must not be empty")
        if not isfinite(self.weight) or self.weight < 0:
            raise ValueError("evidence weight must be finite and non-negative")


@dataclass(frozen=True)
class Contradiction:
    """A typed counter-observation; it prevents silent promotion."""

    source: str
    reason: str
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    severity: float = 1.0

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.reason.strip():
            raise ValueError("contradictions require source and reason")
        if not isfinite(self.severity) or not 0 <= self.severity <= 1:
            raise ValueError("contradiction severity must be between 0 and 1")


@dataclass(frozen=True)
class TypedMemory:
    """A memory with an explicit label, provenance, and freshness state."""

    memory_id: str
    content: str
    label: MemoryLabel
    evidence: tuple[Evidence, ...] = ()
    contradictions: tuple[Contradiction, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_validated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.memory_id.strip() or not self.content.strip():
            raise ValueError("typed memories require an id and content")

    @property
    def support_score(self) -> float:
        return sum(e.weight for e in self.evidence if e.kind != EvidenceKind.CONTRADICTION)

    @property
    def contradiction_score(self) -> float:
        return sum(c.severity for c in self.contradictions)

    @property
    def is_procedural(self) -> bool:
        return self.label is MemoryLabel.PROCEDURAL

    def age(self, now: datetime | None = None) -> timedelta:
        reference = self.last_validated_at or self.created_at
        return max(timedelta(0), _utc(now) - _utc(reference))

    def is_stale(self, *, now: datetime | None = None, max_age: timedelta) -> bool:
        if max_age < timedelta(0):
            raise ValueError("max_age must not be negative")
        return self.age(now) > max_age

