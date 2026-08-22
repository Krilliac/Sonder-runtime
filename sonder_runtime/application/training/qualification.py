"""Fail-closed training qualification, evaluation, and learning-choice policy."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Iterable, Mapping, Protocol


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
        """Require an exact training lock and execution environment.

        Ordering in an installer report is not meaningful, but membership and
        every field in each dependency are.  Duplicate package names are
        rejected rather than normalized away so a malformed report cannot look
        like a valid lock.
        """
        actual_input = tuple(installed)
        actual_names = tuple(item.name for item in actual_input)
        if len(set(actual_names)) != len(actual_names):
            return False
        actual = tuple(sorted(actual_input, key=lambda item: item.name))
        return environment_digest == self.environment_digest and actual == self.dependencies

    def verify_exact(
        self, *, environment_digest: str, installed: Iterable[LockedDependency]
    ) -> tuple[bool, tuple[str, ...]]:
        """Return auditable mismatch reasons without exposing package content."""
        actual_input = tuple(installed)
        failures: list[str] = []
        if environment_digest != self.environment_digest:
            failures.append("environment_mismatch")
        names = tuple(item.name for item in actual_input)
        if len(set(names)) != len(names):
            failures.append("duplicate_installed_dependency")
        expected_names = {item.name for item in self.dependencies}
        actual_names = set(names)
        if expected_names - actual_names:
            failures.append("missing_dependency")
        if actual_names - expected_names:
            failures.append("extra_dependency")
        if tuple(sorted(actual_input, key=lambda item: item.name)) != self.dependencies:
            failures.append("dependency_record_mismatch")
        return not failures, tuple(failures)


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
    privacy_status: str = "approved"
    source_revision: str = ""
    dedup_method: str = ""
    dedup_snapshot_digest: str = ""
    train_eval_overlap_count: int = 0

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.dataset_id, self.snapshot_digest, self.source, self.license_id, self.privacy_review_id, self.dedup_digest, self.train_snapshot_digest, self.eval_snapshot_digest)):
            raise ValueError("dataset provenance, privacy, license, and snapshot fields are required")
        if (
            self.row_count < 0
            or self.duplicate_count < 0
            or self.contamination_count < 0
            or self.train_eval_overlap_count < 0
        ):
            raise ValueError("dataset counts must be non-negative")

    def validate(self, *, allowed_licenses: frozenset[str], approved_sources: frozenset[str], require_private_review: bool = True) -> tuple[str, ...]:
        failures: list[str] = []
        if self.license_id not in allowed_licenses:
            failures.append("license_not_allowed")
        if self.source not in approved_sources:
            failures.append("source_not_approved")
        if not self.source_revision.strip():
            failures.append("source_revision_missing")
        if require_private_review and not self.privacy_review_id.strip():
            failures.append("privacy_review_missing")
        if self.privacy_status != "approved":
            failures.append("privacy_not_approved")
        if not self.dedup_method.strip() or not self.dedup_snapshot_digest.strip():
            failures.append("dedup_evidence_missing")
        elif self.dedup_snapshot_digest != self.snapshot_digest:
            failures.append("dedup_snapshot_mismatch")
        if self.duplicate_count or self.contamination_count or self.train_eval_overlap_count:
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

    def __post_init__(self) -> None:
        values = (
            self.behavior_score,
            self.regression_delta,
            self.latency_ms,
            self.memory_mb,
            self.context_score,
            self.tool_use_score,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("training evaluation metrics must be finite")
        if self.latency_ms < 0 or self.memory_mb < 0 or self.regression_delta < 0:
            raise ValueError("training resource and regression metrics cannot be negative")


@dataclass(frozen=True)
class TrainingEvaluationPolicy:
    minimum_behavior: float = 0.8
    maximum_regression: float = 0.05
    maximum_latency_ms: float = 1000.0
    maximum_memory_mb: float = 4096.0
    minimum_context: float = 0.8
    minimum_tool_use: float = 0.8

    def __post_init__(self) -> None:
        thresholds = (
            self.minimum_behavior,
            self.maximum_regression,
            self.maximum_latency_ms,
            self.maximum_memory_mb,
            self.minimum_context,
            self.minimum_tool_use,
        )
        if not all(math.isfinite(value) for value in thresholds):
            raise ValueError("training gate thresholds must be finite")
        if any(value < 0 for value in (self.maximum_regression, self.maximum_latency_ms, self.maximum_memory_mb)):
            raise ValueError("training gate upper bounds cannot be negative")

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


class LearningMethodPort(Protocol):
    """Port implemented by memory/retrieval/skill/routing/few-shot adapters."""

    def can_encode(self, behavior: str) -> bool: ...

    def apply(self, behavior: str) -> str: ...


class WeightTrainingPort(Protocol):
    """Existing attended training boundary used only after cheap methods fail."""

    def train(self, behavior: str) -> str: ...


class CheapLearningFirst:
    """Select the least invasive mechanism that can encode the behavior."""

    _ORDER = ("memory", "retrieval", "skill", "routing", "few_shot", "weight_training")

    def choose(self, *, reliable_methods: Iterable[str], behavior: str) -> LearningChoice:
        available = frozenset(reliable_methods)
        for method in self._ORDER:
            if method in available:
                return LearningChoice(method, f"{method} is the least invasive reliable mechanism for {behavior}")
        return LearningChoice("weight_training", f"no cheaper reliable mechanism was qualified for {behavior}")

    def execute(
        self,
        *,
        behavior: str,
        methods: Mapping[str, LearningMethodPort],
        weight_training: WeightTrainingPort,
    ) -> LearningChoice:
        """Apply the first reliable cheap port; train only as the final fallback."""
        for method in self._ORDER[:-1]:
            port = methods.get(method)
            if port is not None and port.can_encode(behavior):
                receipt = port.apply(behavior)
                if not isinstance(receipt, str) or not receipt.strip():
                    raise ValueError(f"{method} port returned no receipt")
                return LearningChoice(method, f"{method} port applied: {receipt}")
        receipt = weight_training.train(behavior)
        if not isinstance(receipt, str) or not receipt.strip():
            raise ValueError("weight training port returned no receipt")
        return LearningChoice("weight_training", f"no cheap port qualified; training applied: {receipt}")


__all__ = [
    "CheapLearningFirst", "DatasetQualification", "LearningChoice",
    "LearningMethodPort", "LockedDependency", "QualifiedDependencyLock",
    "TrainingEvaluation", "TrainingEvaluationPolicy", "WeightTrainingPort",
]
