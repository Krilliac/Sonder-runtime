"""Evidence-gated learning ladder: observation -> candidate -> policy."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
import hashlib
from typing import Iterable

MAX_OBSERVATIONS = 10_000


class LearningStage(IntEnum):
    OBSERVATION = 0
    CANDIDATE = 1
    FACT = 2
    HEURISTIC = 3
    POLICY = 4


@dataclass(frozen=True, slots=True)
class LearningObservation:
    observation_id: str
    content: str
    source: str
    independent_key: str
    provenance: tuple[str, ...] = ()
    confidence: float = 0.0
    observed_at: datetime = datetime.min.replace(tzinfo=timezone.utc)
    positive: bool = True
    explicit_confirmation: bool = False
    evaluation_passed: bool = False
    trusted_source: bool = False

    def __post_init__(self) -> None:
        for name in ("observation_id", "content", "source", "independent_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise ValueError(f"{name} must be bounded non-empty text")
        if (not self.provenance or len(self.provenance) > 16
                or any(not isinstance(item, str) or not item.strip() or len(item) > 512
                       for item in self.provenance)):
            raise ValueError("observation provenance is required")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        for name in ("positive", "explicit_confirmation", "evaluation_passed", "trusted_source"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")

    @property
    def content_key(self) -> str:
        normalized = " ".join(self.content.split()).casefold()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LearningDecision:
    content_key: str
    stage: LearningStage
    observation_ids: tuple[str, ...]
    independent_sources: tuple[str, ...]
    reason: str
    contradiction_count: int
    untrusted_count: int

    @property
    def promotable(self) -> bool:
        return self.stage >= LearningStage.FACT


@dataclass(frozen=True, slots=True)
class LearningLadderPolicy:
    fact_sources: int = 2
    heuristic_sources: int = 3
    policy_sources: int = 5
    minimum_confidence: float = 0.70

    def __post_init__(self) -> None:
        if not (1 <= self.fact_sources <= self.heuristic_sources <= self.policy_sources <= 16):
            raise ValueError("source thresholds must be increasing")
        if not 0.0 <= self.minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence must be between 0 and 1")


class LearningLadder:
    """Evaluate immutable observations without mutating memory or policy."""

    def __init__(self, policy: LearningLadderPolicy | None = None) -> None:
        self.policy = policy or LearningLadderPolicy()

    def evaluate(self, observations: Iterable[LearningObservation]) -> tuple[LearningDecision, ...]:
        grouped: dict[str, list[LearningObservation]] = {}
        for index, observation in enumerate(observations):
            if index >= MAX_OBSERVATIONS:
                raise ValueError("too many learning observations")
            grouped.setdefault(observation.content_key, []).append(observation)
        decisions: list[LearningDecision] = []
        for key, values in grouped.items():
            ordered = tuple(sorted(values, key=lambda item: (item.observed_at, item.observation_id)))
            positive = tuple(item for item in ordered if item.positive)
            negative = tuple(item for item in ordered if not item.positive)
            trusted = tuple(item for item in positive if item.trusted_source and item.confidence >= self.policy.minimum_confidence)
            sources = tuple(sorted({item.independent_key for item in trusted}))
            untrusted_count = sum(not item.trusted_source for item in positive)
            if negative:
                stage, reason = LearningStage.CANDIDATE, "contradictory evidence demotes promotion"
            elif not positive:
                stage, reason = LearningStage.OBSERVATION, "no positive evidence"
            elif len(sources) >= self.policy.policy_sources and any(
                item.explicit_confirmation and item.evaluation_passed for item in trusted
            ):
                stage, reason = LearningStage.POLICY, "independent evidence, confirmation, and evaluation"
            elif len(sources) >= self.policy.heuristic_sources:
                stage, reason = LearningStage.HEURISTIC, "independent trusted evidence supports a heuristic"
            elif len(sources) >= self.policy.fact_sources:
                stage, reason = LearningStage.FACT, "independent trusted evidence supports a fact"
            else:
                stage, reason = LearningStage.CANDIDATE, "insufficient independent trusted evidence"
            decisions.append(LearningDecision(key, stage, tuple(item.observation_id for item in ordered), sources, reason, len(negative), untrusted_count))
        return tuple(sorted(decisions, key=lambda item: item.content_key))


__all__ = [
    "LearningDecision", "LearningLadder", "LearningLadderPolicy", "LearningObservation",
    "LearningStage", "MAX_OBSERVATIONS",
]
