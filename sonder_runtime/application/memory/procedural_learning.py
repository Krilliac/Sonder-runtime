"""Promotion policy for evidence-backed procedural memories."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ...domain.memory.wp6_typed import MemoryLabel, TypedMemory


@dataclass(frozen=True)
class PromotionCandidate:
    memory_id: str
    content: str
    label: MemoryLabel
    evidence_score: float
    support_count: int
    contradiction_count: int


@dataclass(frozen=True)
class PromotionPolicy:
    min_support_score: float = 1.0
    max_age: timedelta = timedelta(days=30)
    max_content_length: int = 4000
    max_candidates: int = 25
    reject_contradictions: bool = True

    def __post_init__(self) -> None:
        if self.min_support_score < 0 or self.max_age < timedelta(0):
            raise ValueError("promotion thresholds must not be negative")
        if self.max_content_length <= 0 or self.max_candidates <= 0:
            raise ValueError("promotion bounds must be positive")


class ProceduralLearningService:
    """Selects bounded candidates without mutating the memory store."""

    def __init__(self, policy: PromotionPolicy | None = None):
        self.policy = policy or PromotionPolicy()

    def candidates(
        self,
        memories: list[TypedMemory] | tuple[TypedMemory, ...],
        *,
        now: datetime | None = None,
    ) -> list[PromotionCandidate]:
        by_content: dict[str, PromotionCandidate] = {}
        for memory in memories:
            if not memory.is_procedural or memory.is_stale(now=now, max_age=self.policy.max_age):
                continue
            if self.policy.reject_contradictions and memory.contradictions:
                continue
            if len(memory.content) > self.policy.max_content_length:
                continue
            if memory.support_score < self.policy.min_support_score:
                continue
            key = " ".join(memory.content.split()).casefold()
            candidate = PromotionCandidate(
                memory_id=memory.memory_id,
                content=memory.content,
                label=memory.label,
                evidence_score=memory.support_score,
                support_count=len(memory.evidence),
                contradiction_count=len(memory.contradictions),
            )
            previous = by_content.get(key)
            if previous is None or (candidate.evidence_score, candidate.memory_id) > (
                previous.evidence_score, previous.memory_id,
            ):
                by_content[key] = candidate
        eligible = list(by_content.values())
        eligible.sort(key=lambda item: (-item.evidence_score, item.memory_id))
        return eligible[: self.policy.max_candidates]
