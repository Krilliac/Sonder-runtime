"""Deterministic scenario, matrix, outcome, and regression evaluation contracts.

The harness is provider-neutral and has no network or model dependency.  A
provider is injected through :class:`EvaluationProvider`; production adapters
retain responsibility for enforcing their own request timeout and privacy
policy.  Reports omit raw case values, while the explicit trajectory carries
the values required for replay and must therefore be persisted deliberately.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from .proposal_lifecycle import (
    EvaluationDimension,
    EvaluationMode,
    EvaluationResult,
    EvaluationSuite,
)
from .trajectory_replay import ReplayReport, TrajectoryRecord, TrajectoryStep, compare_trajectories


SCENARIO_SCHEMA = "sonder.evaluation-scenario.v1"
RUN_SCHEMA = "sonder.reproducible-evaluation-run.v1"
MATRIX_SCHEMA = "sonder.reproducible-evaluation-matrix.v1"
MAX_CASES = 512
MAX_TARGETS = 32
MAX_VALUE_BYTES = 64 * 1024
MAX_TIMEOUT_MS = 30 * 60 * 1000


class ReproducibleEvaluationError(ValueError):
    """Invalid, unbounded, incomparable, or tampered evaluation evidence."""


class ProviderFailure(RuntimeError):
    """Expected provider failure with a stable machine-readable category."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = _text(code, "provider failure code", 64)


class OutcomeStatus(str, Enum):
    PASSED = "passed"
    ASSERTION_FAILED = "assertion_failed"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    INVALID_RESPONSE = "invalid_response"
    GRADER_ERROR = "grader_error"


class ErrorCategory(str, Enum):
    NONE = "none"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_PROTOCOL = "provider_protocol"
    PROVIDER_INTERNAL = "provider_internal"
    INVALID_RESPONSE = "invalid_response"
    GRADER_FAILURE = "grader_failure"


@dataclass(frozen=True)
class RegressionThresholds:
    """Absolute gates plus a comparable-baseline regression allowance."""

    min_pass_rate: float = 1.0
    max_timeout_rate: float = 0.0
    max_error_rate: float = 0.0
    max_pass_rate_drop: float = 0.0
    max_case_regressions: int = 0

    def __post_init__(self) -> None:
        for name in ("min_pass_rate", "max_timeout_rate", "max_error_rate", "max_pass_rate_drop"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ReproducibleEvaluationError(f"{name} must be a finite rate in [0, 1]")
            object.__setattr__(self, name, float(value))
        if type(self.max_case_regressions) is not int or self.max_case_regressions < 0:
            raise ReproducibleEvaluationError("max_case_regressions must be a non-negative integer")

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_pass_rate": self.min_pass_rate,
            "max_timeout_rate": self.max_timeout_rate,
            "max_error_rate": self.max_error_rate,
            "max_pass_rate_drop": self.max_pass_rate_drop,
            "max_case_regressions": self.max_case_regressions,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RegressionThresholds":
        _keys(value, set(cls.__dataclass_fields__), "thresholds")
        try:
            return cls(**dict(value))
        except TypeError as exc:
            raise ReproducibleEvaluationError("thresholds are malformed") from exc


@dataclass(frozen=True)
class ScenarioCase:
    case_id: str
    input: Any
    expected_output: Any
    tags: tuple[str, ...] = ()
    timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", _text(self.case_id, "case_id", 128))
        _bounded_value(self.input, "case input")
        _bounded_value(self.expected_output, "expected output")
        object.__setattr__(self, "input", _freeze_json(self.input))
        object.__setattr__(self, "expected_output", _freeze_json(self.expected_output))
        if type(self.timeout_ms) is not int or not 1 <= self.timeout_ms <= MAX_TIMEOUT_MS:
            raise ReproducibleEvaluationError("case timeout_ms is outside the bounded range")
        tags = tuple(_text(item, "case tag", 64) for item in self.tags)
        if tags != tuple(sorted(set(tags))) or len(tags) > 32:
            raise ReproducibleEvaluationError("case tags must be unique, sorted, and bounded")
        object.__setattr__(self, "tags", tags)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "input": _json_value(self.input),
            "expected_output": _json_value(self.expected_output),
            "tags": list(self.tags),
            "timeout_ms": self.timeout_ms,
        }


@dataclass(frozen=True)
class EvaluationScenario:
    scenario_id: str
    version: str
    cases: tuple[ScenarioCase, ...]
    thresholds: RegressionThresholds = field(default_factory=RegressionThresholds)
    description: str = ""
    schema: str = SCENARIO_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_id", _text(self.scenario_id, "scenario_id", 128))
        object.__setattr__(self, "version", _text(self.version, "scenario version", 64))
        if self.schema != SCENARIO_SCHEMA:
            raise ReproducibleEvaluationError("unsupported scenario schema")
        if not isinstance(self.cases, tuple) or not 1 <= len(self.cases) <= MAX_CASES:
            raise ReproducibleEvaluationError(f"scenario must contain 1..{MAX_CASES} cases")
        ids = tuple(item.case_id for item in self.cases)
        if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
            raise ReproducibleEvaluationError("scenario case IDs must be unique and sorted")
        if not isinstance(self.thresholds, RegressionThresholds):
            raise ReproducibleEvaluationError("scenario thresholds are invalid")
        if not isinstance(self.description, str) or len(self.description) > 2_048:
            raise ReproducibleEvaluationError("scenario description must be bounded text")

    @property
    def digest(self) -> str:
        return _digest(self.as_dict(include_digest=False))

    def as_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": self.schema,
            "scenario_id": self.scenario_id,
            "version": self.version,
            "description": self.description,
            "thresholds": self.thresholds.as_dict(),
            "cases": [case.as_dict() for case in self.cases],
        }
        if include_digest:
            value["scenario_digest"] = self.digest
        return value

    def as_suite(self) -> EvaluationSuite:
        """Project this scenario into the existing proposal/result vocabulary."""
        return EvaluationSuite(
            self.scenario_id,
            self.version,
            (EvaluationDimension("fixture", self.digest),),
            ("error_rate", "pass_rate", "timeout_rate"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvaluationScenario":
        allowed = {"schema", "scenario_id", "version", "description", "thresholds", "cases", "scenario_digest"}
        _keys(value, allowed, "scenario")
        try:
            cases = tuple(
                _case_from_dict(item)
                for item in value["cases"]
            )
            scenario = cls(
                scenario_id=value["scenario_id"], version=value["version"], cases=cases,
                thresholds=RegressionThresholds.from_dict(value.get("thresholds", {})),
                description=value.get("description", ""), schema=value.get("schema", ""),
            )
        except (KeyError, TypeError) as exc:
            raise ReproducibleEvaluationError("scenario fixture is malformed") from exc
        supplied = value.get("scenario_digest")
        if supplied is not None and supplied != scenario.digest:
            raise ReproducibleEvaluationError("scenario fixture digest mismatch")
        return scenario


@dataclass(frozen=True)
class ProviderIdentity:
    provider_id: str
    model_id: str
    revision: str
    provider_digest: str

    def __post_init__(self) -> None:
        for name in ("provider_id", "model_id", "revision"):
            object.__setattr__(self, name, _text(getattr(self, name), name, 256))
        if not _is_digest(self.provider_digest):
            raise ReproducibleEvaluationError("provider_digest must be a SHA-256 digest")

    @property
    def key(self) -> str:
        return f"{self.provider_id}/{self.model_id}@{self.revision}#{self.provider_digest}"

    def as_dict(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "revision": self.revision,
            "provider_digest": self.provider_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProviderIdentity":
        _keys(value, {"provider_id", "model_id", "revision", "provider_digest"}, "provider identity")
        try:
            return cls(**dict(value))
        except TypeError as exc:
            raise ReproducibleEvaluationError("provider identity is malformed") from exc


@dataclass(frozen=True)
class ProviderResponse:
    output: Any
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0

    def __post_init__(self) -> None:
        _bounded_value(self.output, "provider output")
        object.__setattr__(self, "output", _freeze_json(self.output))
        if isinstance(self.latency_ms, bool) or not isinstance(self.latency_ms, (int, float)) or not math.isfinite(self.latency_ms) or self.latency_ms < 0:
            raise ReproducibleEvaluationError("provider latency_ms must be finite and non-negative")
        for name in ("tokens_in", "tokens_out"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ReproducibleEvaluationError(f"provider {name} must be a non-negative integer")


class EvaluationProvider(Protocol):
    identity: ProviderIdentity

    def invoke(self, request: Any, *, timeout_ms: int) -> ProviderResponse: ...


class EvaluationScenarioRegistry:
    """Immutable-by-identity in-memory scenario registry."""

    def __init__(self) -> None:
        self._scenarios: dict[tuple[str, str], EvaluationScenario] = {}

    def register(self, scenario: EvaluationScenario) -> EvaluationScenario:
        key = (scenario.scenario_id, scenario.version)
        existing = self._scenarios.get(key)
        if existing is not None and existing.digest != scenario.digest:
            raise ReproducibleEvaluationError(f"scenario {scenario.scenario_id!r} version {scenario.version!r} is immutable")
        self._scenarios[key] = scenario
        return scenario

    def resolve(self, scenario_id: str, version: str) -> EvaluationScenario | None:
        return self._scenarios.get((scenario_id, version))

    def inventory(self) -> tuple[dict[str, str], ...]:
        return tuple(
            {"scenario_id": item.scenario_id, "version": item.version, "scenario_digest": item.digest}
            for item in sorted(self._scenarios.values(), key=lambda item: (item.scenario_id, item.version))
        )


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    status: OutcomeStatus
    error_category: ErrorCategory
    expected_digest: str
    actual_digest: str
    latency_ms: float
    tokens_in: int
    tokens_out: int
    diagnostic: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", _text(self.case_id, "case outcome ID", 128))
        if not isinstance(self.status, OutcomeStatus) or not isinstance(self.error_category, ErrorCategory):
            raise ReproducibleEvaluationError("case outcome classification is invalid")
        if not _is_digest(self.expected_digest):
            raise ReproducibleEvaluationError("case expected_digest must be a SHA-256 digest")
        if self.actual_digest and not _is_digest(self.actual_digest):
            raise ReproducibleEvaluationError("case actual_digest must be empty or a SHA-256 digest")
        if isinstance(self.latency_ms, bool) or not isinstance(self.latency_ms, (int, float)) or not math.isfinite(self.latency_ms) or self.latency_ms < 0:
            raise ReproducibleEvaluationError("case latency_ms must be finite and non-negative")
        object.__setattr__(self, "latency_ms", float(self.latency_ms))
        for name in ("tokens_in", "tokens_out"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ReproducibleEvaluationError(f"case {name} must be a non-negative integer")
        if not isinstance(self.diagnostic, str) or len(self.diagnostic) > 256 or any(character in self.diagnostic for character in "\r\n\x00"):
            raise ReproducibleEvaluationError("case diagnostic must be bounded single-line text")

    @property
    def passed(self) -> bool:
        return self.status is OutcomeStatus.PASSED

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "status": self.status.value,
            "error_category": self.error_category.value,
            "expected_digest": self.expected_digest,
            "actual_digest": self.actual_digest,
            "latency_ms": self.latency_ms,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "diagnostic": self.diagnostic,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CaseOutcome":
        _keys(value, {
            "case_id", "status", "error_category", "expected_digest", "actual_digest",
            "latency_ms", "tokens_in", "tokens_out", "diagnostic",
        }, "case outcome")
        try:
            return cls(
                case_id=value["case_id"], status=OutcomeStatus(value["status"]),
                error_category=ErrorCategory(value["error_category"]),
                expected_digest=value["expected_digest"], actual_digest=value["actual_digest"],
                latency_ms=value["latency_ms"], tokens_in=value["tokens_in"],
                tokens_out=value["tokens_out"], diagnostic=value.get("diagnostic", ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReproducibleEvaluationError("case outcome is malformed") from exc


@dataclass(frozen=True)
class RegressionAssessment:
    passed: bool
    reason_codes: tuple[str, ...]
    regressed_case_ids: tuple[str, ...] = ()
    baseline_run_id: str = ""
    baseline_pass_rate: float | None = None

    def __post_init__(self) -> None:
        if type(self.passed) is not bool:
            raise ReproducibleEvaluationError("assessment passed must be boolean")
        reasons = tuple(_text(item, "assessment reason", 64) for item in self.reason_codes)
        regressed = tuple(_text(item, "regressed case ID", 128) for item in self.regressed_case_ids)
        if len(reasons) != len(set(reasons)) or regressed != tuple(sorted(set(regressed))):
            raise ReproducibleEvaluationError("assessment reasons must be unique and case IDs must be unique and sorted")
        if bool(self.baseline_run_id) != (self.baseline_pass_rate is not None):
            raise ReproducibleEvaluationError("assessment baseline ID and pass rate must be supplied together")
        if self.baseline_run_id and not _is_digest(self.baseline_run_id):
            raise ReproducibleEvaluationError("assessment baseline_run_id must be a SHA-256 digest")
        if self.baseline_pass_rate is not None:
            value = self.baseline_pass_rate
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ReproducibleEvaluationError("assessment baseline_pass_rate must be a finite rate in [0, 1]")
            object.__setattr__(self, "baseline_pass_rate", float(value))
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "regressed_case_ids", regressed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reason_codes": list(self.reason_codes),
            "regressed_case_ids": list(self.regressed_case_ids),
            "baseline_run_id": self.baseline_run_id or None,
            "baseline_pass_rate": self.baseline_pass_rate,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RegressionAssessment":
        _keys(value, {"passed", "reason_codes", "regressed_case_ids", "baseline_run_id", "baseline_pass_rate"}, "regression assessment")
        try:
            if type(value["passed"]) is not bool:
                raise ReproducibleEvaluationError("assessment passed must be boolean")
            return cls(
                value["passed"], tuple(value["reason_codes"]), tuple(value.get("regressed_case_ids", ())),
                value.get("baseline_run_id") or "", value.get("baseline_pass_rate"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReproducibleEvaluationError("regression assessment is malformed") from exc


@dataclass(frozen=True)
class EvaluationRunReport:
    scenario: EvaluationScenario
    target: ProviderIdentity
    outcomes: tuple[CaseOutcome, ...]
    trajectory: TrajectoryRecord
    assessment: RegressionAssessment
    schema: str = RUN_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != RUN_SCHEMA:
            raise ReproducibleEvaluationError("unsupported evaluation run schema")
        if not self.outcomes or len(self.outcomes) != len(self.scenario.cases):
            raise ReproducibleEvaluationError("run outcomes must cover every scenario case")
        if tuple(item.case_id for item in self.outcomes) != tuple(item.case_id for item in self.scenario.cases):
            raise ReproducibleEvaluationError("run outcome order must match the scenario")
        if len(self.trajectory.steps) != len(self.outcomes):
            raise ReproducibleEvaluationError("run trajectory must cover every outcome")
        if any(item not in {case.case_id for case in self.scenario.cases} for item in self.assessment.regressed_case_ids):
            raise ReproducibleEvaluationError("assessment names an unknown regressed case")
        if self.assessment.passed is not (not self.assessment.reason_codes):
            raise ReproducibleEvaluationError("assessment pass flag contradicts its reason codes")
        metadata = dict(self.trajectory.metadata or {})
        if metadata.get("scenario_digest") != self.scenario.digest or metadata.get("target") != self.target.as_dict():
            raise ReproducibleEvaluationError("run trajectory identity does not match the report")

    @property
    def pass_rate(self) -> float:
        return sum(item.passed for item in self.outcomes) / len(self.outcomes)

    @property
    def timeout_rate(self) -> float:
        return sum(item.status is OutcomeStatus.TIMEOUT for item in self.outcomes) / len(self.outcomes)

    @property
    def error_rate(self) -> float:
        errors = {OutcomeStatus.PROVIDER_ERROR, OutcomeStatus.INVALID_RESPONSE, OutcomeStatus.GRADER_ERROR}
        return sum(item.status in errors for item in self.outcomes) / len(self.outcomes)

    @property
    def digest(self) -> str:
        return _digest(self.as_dict(include_digest=False))

    @property
    def run_id(self) -> str:
        return self.digest

    def as_dict(self, *, include_digest: bool = True, include_trace: bool = True) -> dict[str, Any]:
        value = {
            "schema": self.schema,
            "scenario": self.scenario.as_dict(),
            "target": self.target.as_dict(),
            "summary": {
                "passed": sum(item.passed for item in self.outcomes), "total": len(self.outcomes),
                "pass_rate": self.pass_rate, "timeout_rate": self.timeout_rate, "error_rate": self.error_rate,
            },
            "outcomes": [item.as_dict() for item in self.outcomes],
            "assessment": self.assessment.as_dict(),
            "trajectory_digest": self.trajectory.digest,
        }
        if include_trace:
            value["trajectory"] = self.trajectory.as_dict()
        if include_digest:
            value["run_id"] = self.digest
        return value

    def as_evaluation_result(
        self,
        result_id: str,
        *,
        baseline: str,
        mode: EvaluationMode = EvaluationMode.OFFLINE,
        replay_equivalent: bool = False,
    ) -> EvaluationResult:
        if type(replay_equivalent) is not bool:
            raise ReproducibleEvaluationError("replay_equivalent must be boolean")
        suite = self.scenario.as_suite()
        return EvaluationResult(
            result_id, suite, self.target.key, baseline, mode, suite.dimensions,
            {"error_rate": self.error_rate, "pass_rate": self.pass_rate, "timeout_rate": self.timeout_rate},
            self.assessment.passed, len(self.outcomes), trajectory_digest=self.trajectory.digest,
            replay_equivalent=replay_equivalent, provenance=(f"evaluation-run:{self.run_id}",),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvaluationRunReport":
        if not isinstance(value, Mapping) or value.get("schema") != RUN_SCHEMA:
            raise ReproducibleEvaluationError("unsupported evaluation run payload")
        _keys(value, {
            "schema", "scenario", "target", "summary", "outcomes", "assessment",
            "trajectory_digest", "trajectory", "run_id",
        }, "evaluation run")
        try:
            scenario = EvaluationScenario.from_dict(value["scenario"])
            target = ProviderIdentity.from_dict(value["target"])
            outcomes = tuple(CaseOutcome.from_dict(item) for item in value["outcomes"])
            trajectory = TrajectoryRecord.from_dict(value["trajectory"])
            assessment = RegressionAssessment.from_dict(value["assessment"])
            report = cls(scenario, target, outcomes, trajectory, assessment)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ReproducibleEvaluationError):
                raise
            raise ReproducibleEvaluationError("evaluation run payload is malformed") from exc
        summary = value.get("summary")
        expected_summary = report.as_dict(include_digest=False)["summary"]
        if summary != expected_summary:
            raise ReproducibleEvaluationError("evaluation run summary mismatch")
        if value.get("trajectory_digest") != report.trajectory.digest:
            raise ReproducibleEvaluationError("evaluation run trajectory digest mismatch")
        if value.get("run_id") != report.digest:
            raise ReproducibleEvaluationError("evaluation run digest mismatch")
        expected_assessment = _assessment_from_evidence(
            report.scenario, report.outcomes,
            baseline_run_id=report.assessment.baseline_run_id,
            baseline_pass_rate=report.assessment.baseline_pass_rate,
            regressed=report.assessment.regressed_case_ids,
        )
        if report.assessment != expected_assessment:
            raise ReproducibleEvaluationError("evaluation run assessment mismatch")
        return report


@dataclass(frozen=True)
class EvaluationMatrixReport:
    scenario_digest: str
    runs: tuple[EvaluationRunReport, ...]
    schema: str = MATRIX_SCHEMA

    def __post_init__(self) -> None:
        if not 1 <= len(self.runs) <= MAX_TARGETS:
            raise ReproducibleEvaluationError(f"matrix must contain 1..{MAX_TARGETS} runs")
        if any(run.scenario.digest != self.scenario_digest for run in self.runs):
            raise ReproducibleEvaluationError("matrix runs must share one exact scenario digest")
        keys = tuple(run.target.key for run in self.runs)
        if keys != tuple(sorted(set(keys))):
            raise ReproducibleEvaluationError("matrix target identities must be unique and sorted")

    @property
    def digest(self) -> str:
        return _digest(self.as_dict(include_digest=False))

    def as_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {"schema": self.schema, "scenario_digest": self.scenario_digest, "runs": [run.as_dict() for run in self.runs]}
        if include_digest:
            value["matrix_id"] = self.digest
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvaluationMatrixReport":
        if not isinstance(value, Mapping) or value.get("schema") != MATRIX_SCHEMA:
            raise ReproducibleEvaluationError("unsupported evaluation matrix payload")
        _keys(value, {"schema", "scenario_digest", "runs", "matrix_id"}, "evaluation matrix")
        try:
            report = cls(
                value["scenario_digest"],
                tuple(EvaluationRunReport.from_dict(item) for item in value["runs"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ReproducibleEvaluationError):
                raise
            raise ReproducibleEvaluationError("evaluation matrix payload is malformed") from exc
        if value.get("matrix_id") != report.digest:
            raise ReproducibleEvaluationError("evaluation matrix digest mismatch")
        return report


class ReproducibleEvaluationRunner:
    """Run exact-match golden scenarios across injected provider/model targets."""

    def run(self, scenario: EvaluationScenario, provider: EvaluationProvider, *, baseline: EvaluationRunReport | None = None) -> EvaluationRunReport:
        if not isinstance(provider.identity, ProviderIdentity):
            raise ReproducibleEvaluationError("provider identity is invalid")
        outcomes: list[CaseOutcome] = []
        steps: list[TrajectoryStep] = []
        for index, case in enumerate(scenario.cases):
            outcome, observed = self._run_case(case, provider)
            outcomes.append(outcome)
            steps.append(TrajectoryStep(
                index, _json_value(case.input), observed,
                {"case_id": case.case_id, "expected_digest": _digest(case.expected_output)},
            ))
        trajectory_id = _digest({"scenario_digest": scenario.digest, "target": provider.identity.as_dict()})
        trajectory = TrajectoryRecord.from_steps(
            trajectory_id, steps,
            metadata={"scenario_id": scenario.scenario_id, "scenario_version": scenario.version,
                      "scenario_digest": scenario.digest, "target": provider.identity.as_dict()},
        )
        assessment = assess_regression(scenario, tuple(outcomes), baseline)
        return EvaluationRunReport(scenario, provider.identity, tuple(outcomes), trajectory, assessment)

    def run_matrix(self, scenario: EvaluationScenario, providers: Sequence[EvaluationProvider], *, baseline: EvaluationRunReport | None = None) -> EvaluationMatrixReport:
        if not isinstance(providers, Sequence) or isinstance(providers, (str, bytes)) or not 1 <= len(providers) <= MAX_TARGETS:
            raise ReproducibleEvaluationError(f"providers must contain 1..{MAX_TARGETS} targets")
        ordered = sorted(providers, key=lambda provider: provider.identity.key)
        keys = [provider.identity.key for provider in ordered]
        if len(keys) != len(set(keys)):
            raise ReproducibleEvaluationError("matrix target identities must be unique")
        return EvaluationMatrixReport(scenario.digest, tuple(self.run(scenario, provider, baseline=baseline) for provider in ordered))

    def replay(self, expected: EvaluationRunReport, provider: EvaluationProvider) -> ReplayReport:
        if provider.identity != expected.target:
            raise ReproducibleEvaluationError("replay provider identity does not match recorded target")
        actual = self.run(expected.scenario, provider)
        return compare_trajectories(expected.trajectory, actual.trajectory)

    @staticmethod
    def _run_case(case: ScenarioCase, provider: EvaluationProvider) -> tuple[CaseOutcome, dict[str, Any]]:
        expected_digest = _digest(case.expected_output)
        try:
            response = provider.invoke(_json_value(case.input), timeout_ms=case.timeout_ms)
            if not isinstance(response, ProviderResponse):
                raise ReproducibleEvaluationError("provider returned a non-ProviderResponse value")
            actual_digest = _digest(response.output)
            passed = _canonical(response.output) == _canonical(case.expected_output)
            status = OutcomeStatus.PASSED if passed else OutcomeStatus.ASSERTION_FAILED
            outcome = CaseOutcome(case.case_id, status, ErrorCategory.NONE, expected_digest, actual_digest,
                                  float(response.latency_ms), response.tokens_in, response.tokens_out)
            return outcome, {"kind": "response", "output": _json_value(response.output)}
        except TimeoutError:
            outcome = CaseOutcome(case.case_id, OutcomeStatus.TIMEOUT, ErrorCategory.DEADLINE_EXCEEDED,
                                  expected_digest, "", 0.0, 0, 0, "provider deadline exceeded")
            return outcome, {"kind": "timeout", "error_category": outcome.error_category.value}
        except ProviderFailure as exc:
            category = ErrorCategory.PROVIDER_UNAVAILABLE if exc.code == "unavailable" else ErrorCategory.PROVIDER_PROTOCOL
            diagnostic = exc.code
            outcome = CaseOutcome(case.case_id, OutcomeStatus.PROVIDER_ERROR, category, expected_digest, "", 0.0, 0, 0, diagnostic)
            return outcome, {"kind": "provider_error", "error_category": category.value}
        except ReproducibleEvaluationError as exc:
            outcome = CaseOutcome(case.case_id, OutcomeStatus.INVALID_RESPONSE, ErrorCategory.INVALID_RESPONSE,
                                  expected_digest, "", 0.0, 0, 0, _safe_diagnostic(str(exc)))
            return outcome, {"kind": "invalid_response", "error_category": outcome.error_category.value}
        except Exception as exc:  # provider boundary: preserve the case and classify without traceback leakage
            outcome = CaseOutcome(case.case_id, OutcomeStatus.PROVIDER_ERROR, ErrorCategory.PROVIDER_INTERNAL,
                                  expected_digest, "", 0.0, 0, 0, type(exc).__name__)
            return outcome, {"kind": "provider_error", "error_category": outcome.error_category.value}


def assess_regression(
    scenario: EvaluationScenario,
    outcomes: tuple[CaseOutcome, ...],
    baseline: EvaluationRunReport | None = None,
) -> RegressionAssessment:
    regressed: tuple[str, ...] = ()
    baseline_run_id = ""
    baseline_pass_rate = None
    if baseline is not None:
        if baseline.scenario.digest != scenario.digest:
            raise ReproducibleEvaluationError("baseline scenario digest does not match candidate scenario")
        base_by_id = {item.case_id: item for item in baseline.outcomes}
        if set(base_by_id) != {item.case_id for item in outcomes}:
            raise ReproducibleEvaluationError("baseline and candidate case sets do not match")
        baseline_run_id = baseline.run_id
        baseline_pass_rate = baseline.pass_rate
        regressed = tuple(item.case_id for item in outcomes if base_by_id[item.case_id].passed and not item.passed)
    return _assessment_from_evidence(
        scenario, outcomes, baseline_run_id=baseline_run_id,
        baseline_pass_rate=baseline_pass_rate, regressed=regressed,
    )


def _assessment_from_evidence(
    scenario: EvaluationScenario,
    outcomes: tuple[CaseOutcome, ...],
    *,
    baseline_run_id: str,
    baseline_pass_rate: float | None,
    regressed: tuple[str, ...],
) -> RegressionAssessment:
    total = len(outcomes)
    passed = sum(item.passed for item in outcomes) / total
    timeout = sum(item.status is OutcomeStatus.TIMEOUT for item in outcomes) / total
    errors = {OutcomeStatus.PROVIDER_ERROR, OutcomeStatus.INVALID_RESPONSE, OutcomeStatus.GRADER_ERROR}
    error = sum(item.status in errors for item in outcomes) / total
    threshold = scenario.thresholds
    reasons: list[str] = []
    if passed < threshold.min_pass_rate:
        reasons.append("minimum_pass_rate")
    if timeout > threshold.max_timeout_rate:
        reasons.append("maximum_timeout_rate")
    if error > threshold.max_error_rate:
        reasons.append("maximum_error_rate")
    if baseline_pass_rate is not None:
        if baseline_pass_rate - passed > threshold.max_pass_rate_drop:
            reasons.append("maximum_pass_rate_drop")
        if len(regressed) > threshold.max_case_regressions:
            reasons.append("maximum_case_regressions")
    return RegressionAssessment(not reasons, tuple(reasons), regressed, baseline_run_id, baseline_pass_rate)


def evaluation_diagnostics(report: EvaluationRunReport) -> dict[str, Any]:
    """Return a compact, raw-value-free diagnostic projection."""
    status_counts = {status.value: 0 for status in OutcomeStatus}
    error_counts = {category.value: 0 for category in ErrorCategory}
    for outcome in report.outcomes:
        status_counts[outcome.status.value] += 1
        error_counts[outcome.error_category.value] += 1
    return {
        "run_id": report.run_id,
        "scenario": f"{report.scenario.scenario_id}@{report.scenario.version}",
        "target": report.target.key,
        "gate_passed": report.assessment.passed,
        "reason_codes": list(report.assessment.reason_codes),
        "status_counts": {key: value for key, value in status_counts.items() if value},
        "error_counts": {key: value for key, value in error_counts.items() if value},
        "failed_case_ids": [item.case_id for item in report.outcomes if not item.passed],
        "regressed_case_ids": list(report.assessment.regressed_case_ids),
    }


def _canonical(value: Any) -> str:
    try:
        return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ReproducibleEvaluationError("evaluation values must be JSON-compatible") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _bounded_value(value: Any, label: str) -> None:
    if len(_canonical(value).encode("utf-8")) > MAX_VALUE_BYTES:
        raise ReproducibleEvaluationError(f"{label} exceeds byte bound")


def _text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ReproducibleEvaluationError(f"{label} must be text")
    value = value.strip()
    if not value or len(value) > limit or any(character in value for character in "\r\n\x00"):
        raise ReproducibleEvaluationError(f"{label} is required and must not exceed {limit} characters")
    return value


def _keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    if not isinstance(value, Mapping):
        raise ReproducibleEvaluationError(f"{label} must be an object")
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ReproducibleEvaluationError(f"{label} contains unsupported fields: {', '.join(unknown)}")


def _case_from_dict(value: Mapping[str, Any]) -> ScenarioCase:
    _keys(value, {"case_id", "input", "expected_output", "tags", "timeout_ms"}, "scenario case")
    try:
        return ScenarioCase(
            case_id=value["case_id"], input=value["input"], expected_output=value["expected_output"],
            tags=tuple(value.get("tags", ())), timeout_ms=value.get("timeout_ms", 30_000),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ReproducibleEvaluationError):
            raise
        raise ReproducibleEvaluationError("scenario case is malformed") from exc


def _is_digest(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


def _safe_diagnostic(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ")[:256]


def _freeze_json(value: Any) -> Any:
    """Detach caller-owned containers and make nested fixture values read-only."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _json_value(value: Any) -> Any:
    """Return ordinary JSON containers for adapters and serialized evidence."""
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


__all__ = [
    "CaseOutcome", "ErrorCategory", "EvaluationMatrixReport", "EvaluationProvider",
    "EvaluationRunReport", "EvaluationScenario", "EvaluationScenarioRegistry",
    "OutcomeStatus", "ProviderFailure", "ProviderIdentity", "ProviderResponse",
    "RegressionAssessment", "RegressionThresholds", "ReproducibleEvaluationError",
    "ReproducibleEvaluationRunner", "ScenarioCase", "assess_regression",
    "evaluation_diagnostics",
]
