"""Fail-closed training qualification, evaluation, and learning-choice policy."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Iterable, Mapping


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LockedDependency:
    name: str
    version: str
    source: str
    artifact_digest: str

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.name, self.version, self.source, self.artifact_digest)):
            raise ValueError("qualified dependency fields are required")

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version, "source": self.source, "artifact_digest": self.artifact_digest}


@dataclass(frozen=True)
class QualifiedDependencyLock:
    dependencies: tuple[LockedDependency, ...]
    lock_id: str
    environment_digest: str

    def __post_init__(self) -> None:
        if not self.lock_id.strip() or not self.environment_digest.strip():
            raise ValueError("lock identity and environment digest are required")
        names = tuple(item.name for item in self.dependencies)
        if names != tuple(sorted(names)) or len(set(names)) != len(names):
            raise ValueError("qualified dependencies must be unique and sorted")

    @property
    def digest(self) -> str:
        return _digest({"lock_id": self.lock_id, "environment_digest": self.environment_digest, "dependencies": [item.as_dict() for item in self.dependencies]})

    def verify(self, *, environment_digest: str, installed: Iterable[LockedDependency]) -> bool:
        actual = tuple(sorted(installed, key=lambda item: item.name))
        return environment_digest == self.environment_digest and actual == self.dependencies


@dataclass(frozen=True)
class DatasetQualification:
    dataset_id: str
    snapshot_digest: str
    source: str
    license_id: str
    privacy_review_id: str
    dedup_digest: str
    train_snapshot_digest: str
    eval_snapshot_digest: str
    row_count: int
    duplicate_count: int = 0
    contamination_count: int = 0

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.dataset_id, self.snapshot_digest, self.source, self.license_id, self.privacy_review_id, self.dedup_digest, self.train_snapshot_digest, self.eval_snapshot_digest)):
            raise ValueError("dataset provenance, privacy, license, and snapshot fields are required")
        if self.row_count < 0 or self.duplicate_count < 0 or self.contamination_count < 0:
            raise ValueError("dataset counts must be non-negative")

    def validate(self, *, allowed_licenses: frozenset[str], approved_sources: frozenset[str], require_private_review: bool = True) -> tuple[str, ...]:
        failures: list[str] = []
        if self.license_id not in allowed_licenses:
            failures.append("license_not_allowed")
        if self.source not in approved_sources:
            failures.append("source_not_approved")
        if require_private_review and not self.privacy_review_id.strip():
            failures.append("privacy_review_missing")
        if self.duplicate_count or self.contamination_count:
            failures.append("dedup_or_contamination_failure")
        if self.train_snapshot_digest == self.eval_snapshot_digest:
            failures.append("train_eval_not_separated")
        if self.row_count == 0:
            failures.append("dataset_empty")
        return tuple(failures)


@dataclass(frozen=True)
class TrainingEvaluation:
    behavior_score: float
    regression_delta: float
    latency_ms: float
    memory_mb: float
    context_score: float
    tool_use_score: float


@dataclass(frozen=True)
class TrainingEvaluationPolicy:
    minimum_behavior: float = 0.8
    maximum_regression: float = 0.05
    maximum_latency_ms: float = 1000.0
    maximum_memory_mb: float = 4096.0
    minimum_context: float = 0.8
    minimum_tool_use: float = 0.8

    def gate(self, result: TrainingEvaluation) -> tuple[bool, tuple[str, ...]]:
        failures: list[str] = []
        if result.behavior_score < self.minimum_behavior: failures.append("behavior")
        if result.regression_delta > self.maximum_regression: failures.append("regression")
        if result.latency_ms > self.maximum_latency_ms: failures.append("latency")
        if result.memory_mb > self.maximum_memory_mb: failures.append("memory")
        if result.context_score < self.minimum_context: failures.append("context")
        if result.tool_use_score < self.minimum_tool_use: failures.append("tool_use")
        return not failures, tuple(failures)


@dataclass(frozen=True)
class LearningChoice:
    method: str
    rationale: str


class CheapLearningFirst:
    """Select the least invasive mechanism that can encode the behavior."""

    _ORDER = ("memory", "retrieval", "skill", "routing", "few_shot", "weight_training")

    def choose(self, *, reliable_methods: Iterable[str], behavior: str) -> LearningChoice:
        available = frozenset(reliable_methods)
        for method in self._ORDER:
            if method in available:
                return LearningChoice(method, f"{method} is the least invasive reliable mechanism for {behavior}")
        return LearningChoice("weight_training", f"no cheaper reliable mechanism was qualified for {behavior}")


__all__ = ["CheapLearningFirst", "DatasetQualification", "LearningChoice", "LockedDependency", "QualifiedDependencyLock", "TrainingEvaluation", "TrainingEvaluationPolicy"]
