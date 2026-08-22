"""Deterministic evaluation trajectories and replay comparison.

This module is deliberately independent of model execution and evaluation-history
storage.  Callers provide the observed step values and, for replay, a function
that produces the candidate output for each recorded input.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable, Mapping, Sequence


SCHEMA = "sonder.evaluation-trajectory.v1"


def _canonical(value: Any) -> str:
    """Serialize JSON-compatible values into a stable comparison form."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise TypeError("trajectory values must be JSON-compatible") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TrajectoryStep:
    """One deterministic input/output observation in an evaluation run."""

    index: int
    input: Any
    output: Any
    state: Any = None

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("trajectory step index must be non-negative")
        _canonical(self.input)
        _canonical(self.output)
        _canonical(self.state)

    @property
    def input_digest(self) -> str:
        return _digest(self.input)

    @property
    def output_digest(self) -> str:
        return _digest(self.output)

    @property
    def state_digest(self) -> str:
        return _digest(self.state)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "input": self.input,
            "output": self.output,
            "state": self.state,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "state_digest": self.state_digest,
        }


@dataclass(frozen=True)
class TrajectoryRecord:
    """Immutable, serializable evaluation trajectory."""

    trajectory_id: str
    steps: tuple[TrajectoryStep, ...]
    metadata: Mapping[str, Any] | None = None
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if not self.trajectory_id:
            raise ValueError("trajectory_id is required")
        if self.schema != SCHEMA:
            raise ValueError(f"unsupported trajectory schema: {self.schema}")
        indexes = tuple(step.index for step in self.steps)
        if indexes != tuple(range(len(self.steps))):
            raise ValueError("trajectory steps must be contiguous and ordered")
        _canonical(dict(self.metadata or {}))

    @classmethod
    def from_steps(
        cls,
        trajectory_id: str,
        steps: Sequence[TrajectoryStep],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> "TrajectoryRecord":
        return cls(trajectory_id, tuple(steps), metadata)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "trajectory_id": self.trajectory_id,
            "metadata": dict(self.metadata or {}),
            "steps": [step.as_dict() for step in self.steps],
            "trajectory_digest": self.digest,
        }

    @property
    def digest(self) -> str:
        payload = {
            "schema": self.schema,
            "trajectory_id": self.trajectory_id,
            "metadata": dict(self.metadata or {}),
            "steps": [step.as_dict() for step in self.steps],
        }
        return _digest(payload)


@dataclass(frozen=True)
class TrajectoryDivergence:
    index: int
    field: str
    expected: Any
    actual: Any


@dataclass(frozen=True)
class ReplayReport:
    trajectory_id: str
    expected_digest: str
    actual_digest: str
    divergences: tuple[TrajectoryDivergence, ...]

    @property
    def equivalent(self) -> bool:
        return not self.divergences


def replay_trajectory(
    expected: TrajectoryRecord,
    evaluator: Callable[[Any], Any],
) -> ReplayReport:
    """Replay recorded inputs and report output/state divergence by step."""
    actual_steps: list[TrajectoryStep] = []
    divergences: list[TrajectoryDivergence] = []
    for step in expected.steps:
        output = evaluator(step.input)
        actual = TrajectoryStep(step.index, step.input, output, step.state)
        actual_steps.append(actual)
        if _canonical(output) != _canonical(step.output):
            divergences.append(TrajectoryDivergence(step.index, "output", step.output, output))
    actual_record = TrajectoryRecord.from_steps(
        expected.trajectory_id, actual_steps, metadata=expected.metadata
    )
    return ReplayReport(expected.trajectory_id, expected.digest, actual_record.digest, tuple(divergences))


def compare_trajectories(expected: TrajectoryRecord, actual: TrajectoryRecord) -> ReplayReport:
    """Compare two immutable records, including identity, metadata, and steps."""
    divergences: list[TrajectoryDivergence] = []
    if expected.trajectory_id != actual.trajectory_id:
        divergences.append(TrajectoryDivergence(-1, "trajectory_id", expected.trajectory_id, actual.trajectory_id))
    if _canonical(dict(expected.metadata or {})) != _canonical(dict(actual.metadata or {})):
        divergences.append(TrajectoryDivergence(-1, "metadata", dict(expected.metadata or {}), dict(actual.metadata or {})))
    if len(expected.steps) != len(actual.steps):
        divergences.append(TrajectoryDivergence(-1, "step_count", len(expected.steps), len(actual.steps)))
    for left, right in zip(expected.steps, actual.steps):
        for field in ("input", "output", "state"):
            expected_value = getattr(left, field)
            actual_value = getattr(right, field)
            if _canonical(expected_value) != _canonical(actual_value):
                divergences.append(TrajectoryDivergence(left.index, field, expected_value, actual_value))
    return ReplayReport(expected.trajectory_id, expected.digest, actual.digest, tuple(divergences))
