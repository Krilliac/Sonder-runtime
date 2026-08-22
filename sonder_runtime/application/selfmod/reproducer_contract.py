"""Typed, non-mutating evidence contracts for self-modification proposals.

The contract makes a self-modification claim reproducible without executing it.
An external runner may later consume the command, but this module deliberately
does not invoke processes, touch files, or perform Git operations.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Final


class ReproducerContractError(ValueError):
    """Raised when evidence is too vague or incomplete to reproduce."""


_SHA256: Final = re.compile(r"^[0-9a-fA-F]{64}$")


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReproducerContractError(f"{field} is required")
    return value.strip()


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    """One independently checkable condition for accepting evidence."""

    criterion_id: str
    description: str
    expected_outcome: str

    def __post_init__(self) -> None:
        _required_text(self.criterion_id, "criterion_id")
        _required_text(self.description, "criterion description")
        _required_text(self.expected_outcome, "criterion expected_outcome")


@dataclass(frozen=True, slots=True)
class ReproducerEvidence:
    """Common immutable evidence required by every reproducer contract."""

    evidence_id: str
    command_argv: tuple[str, ...]
    expected_outcome: str
    artifact_digest: str
    acceptance_criteria: tuple[AcceptanceCriterion, ...]

    def __post_init__(self) -> None:
        _required_text(self.evidence_id, "evidence_id")
        if not isinstance(self.command_argv, tuple) or not self.command_argv:
            raise ReproducerContractError("command_argv must be a non-empty tuple")
        if any(not isinstance(arg, str) or not arg.strip() for arg in self.command_argv):
            raise ReproducerContractError("command_argv must contain non-empty strings")
        _required_text(self.expected_outcome, "expected_outcome")
        if not isinstance(self.artifact_digest, str) or not _SHA256.fullmatch(self.artifact_digest):
            raise ReproducerContractError("artifact_digest must be a 64-character SHA-256 hex digest")
        if not isinstance(self.acceptance_criteria, tuple) or not self.acceptance_criteria:
            raise ReproducerContractError("at least one acceptance criterion is required")
        if any(not isinstance(item, AcceptanceCriterion) for item in self.acceptance_criteria):
            raise ReproducerContractError("acceptance_criteria must contain AcceptanceCriterion values")
        ids = tuple(item.criterion_id for item in self.acceptance_criteria)
        if len(set(ids)) != len(ids):
            raise ReproducerContractError("acceptance criterion ids must be unique")


@dataclass(frozen=True, slots=True)
class FailureEvidence(ReproducerEvidence):
    """Evidence for a concrete, repeatable failure mode."""

    failure_signature: str = ""

    def __post_init__(self) -> None:
        ReproducerEvidence.__post_init__(self)
        _required_text(self.failure_signature, "failure_signature")


@dataclass(frozen=True, slots=True)
class BenchmarkEvidence(ReproducerEvidence):
    """Evidence for a concrete benchmark measurement and comparison."""

    metric: str = ""
    observed_value: float = 0.0
    baseline_value: float | None = None
    unit: str = ""

    def __post_init__(self) -> None:
        ReproducerEvidence.__post_init__(self)
        _required_text(self.metric, "metric")
        _required_text(self.unit, "unit")
        if isinstance(self.observed_value, bool) or not isinstance(self.observed_value, (int, float)):
            raise ReproducerContractError("observed_value must be numeric")
        if self.baseline_value is not None and (
            isinstance(self.baseline_value, bool)
            or not isinstance(self.baseline_value, (int, float))
        ):
            raise ReproducerContractError("baseline_value must be numeric when provided")


class ReproducerContractService:
    """Validate typed evidence without executing or persisting anything."""

    def validate_failure(self, evidence: FailureEvidence) -> FailureEvidence:
        self._validate(evidence, FailureEvidence)
        return evidence

    def validate_benchmark(self, evidence: BenchmarkEvidence) -> BenchmarkEvidence:
        self._validate(evidence, BenchmarkEvidence)
        return evidence

    @staticmethod
    def _validate(evidence: object, expected_type: type[ReproducerEvidence]) -> None:
        if not isinstance(evidence, expected_type):
            raise ReproducerContractError(
                f"expected {expected_type.__name__}, got {type(evidence).__name__}"
            )


__all__ = [
    "AcceptanceCriterion",
    "BenchmarkEvidence",
    "FailureEvidence",
    "ReproducerContractError",
    "ReproducerContractService",
]
