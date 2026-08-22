"""SELFMOD-001: concrete reproducer evidence is required before acceptance."""
from __future__ import annotations

import pytest

from sonder_runtime.application.selfmod.reproducer_contract import (
    AcceptanceCriterion,
    BenchmarkEvidence,
    FailureEvidence,
    ReproducerContractError,
    ReproducerContractService,
)


DIGEST = "a" * 64


def _criterion() -> AcceptanceCriterion:
    return AcceptanceCriterion(
        criterion_id="failure-reproduces",
        description="The guarded reproducer reports the known failure signature.",
        expected_outcome="exit status 1 and signature E-TIMEOUT",
    )


def _common() -> dict[str, object]:
    return {
        "evidence_id": "selfmod-001-failure",
        "command_argv": ("python", "-m", "pytest", "tests/test_target.py", "-q"),
        "expected_outcome": "exit status 1 and signature E-TIMEOUT",
        "artifact_digest": DIGEST,
        "acceptance_criteria": (_criterion(),),
    }


def test_missing_concrete_failure_evidence_is_rejected() -> None:
    with pytest.raises(ReproducerContractError, match="command_argv"):
        FailureEvidence(**{**_common(), "command_argv": ()})

    with pytest.raises(ReproducerContractError, match="artifact_digest"):
        FailureEvidence(**{**_common(), "artifact_digest": "missing"})

    with pytest.raises(ReproducerContractError, match="acceptance criterion"):
        FailureEvidence(**{**_common(), "acceptance_criteria": ()})


def test_valid_failure_evidence_is_accepted_without_execution() -> None:
    evidence = FailureEvidence(**{**_common(), "failure_signature": "E-TIMEOUT"})

    assert ReproducerContractService().validate_failure(evidence) is evidence
    assert evidence.command_argv == ("python", "-m", "pytest", "tests/test_target.py", "-q")
    assert evidence.artifact_digest == DIGEST


def test_valid_benchmark_evidence_is_accepted() -> None:
    evidence = BenchmarkEvidence(**{
        **_common(),
        "evidence_id": "selfmod-001-benchmark",
        "expected_outcome": "p95 latency is at most 100 ms",
        "acceptance_criteria": (AcceptanceCriterion(
            criterion_id="latency-target",
            description="The benchmark remains within the approved latency budget.",
            expected_outcome="p95 <= 100 ms",
        ),),
        "metric": "request_latency_p95",
        "observed_value": 82.5,
        "baseline_value": 97.0,
        "unit": "ms",
    })

    assert ReproducerContractService().validate_benchmark(evidence) is evidence
    assert evidence.observed_value < evidence.baseline_value


def test_validation_rejects_wrong_typed_evidence() -> None:
    with pytest.raises(ReproducerContractError, match="FailureEvidence"):
        ReproducerContractService().validate_failure(object())

    with pytest.raises(ReproducerContractError, match="BenchmarkEvidence"):
        ReproducerContractService().validate_benchmark(object())
