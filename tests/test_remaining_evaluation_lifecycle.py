from __future__ import annotations

from dataclasses import replace
import pytest

from sonder_runtime.application.evaluation.proposal_lifecycle import (
    EvaluationDimension,
    EvaluationLifecycleError,
    EvaluationMode,
    EvaluationResult,
    EvaluationSuite,
    ProposalLifecycle,
    ProposalState,
    ShadowCanaryObservation,
)


def _suite() -> EvaluationSuite:
    return EvaluationSuite(
        "quality", "v3",
        (EvaluationDimension("hardware", "rtx5070ti"), EvaluationDimension("split", "holdout")),
        ("accuracy", "latency_ms"),
    )


def _result(suite: EvaluationSuite, result_id: str, mode: EvaluationMode = EvaluationMode.OFFLINE) -> EvaluationResult:
    return EvaluationResult(
        result_id, suite, "candidate@sha", "baseline@sha", mode, suite.dimensions,
        {"accuracy": 0.97, "latency_ms": 12}, True, 25,
        trajectory_digest="trajectory-digest", replay_equivalent=True, provenance=("runner:local",),
    )


def _lifecycle() -> tuple[ProposalLifecycle, EvaluationSuite]:
    suite = _suite()
    lifecycle = ProposalLifecycle()
    lifecycle.create("p1", "candidate@sha", "baseline@sha", suite)
    lifecycle.submit("p1")
    lifecycle.begin_evaluation("p1")
    lifecycle.record_result("p1", _result(suite, "r1"))
    lifecycle.begin_shadow("p1")
    lifecycle.record_observation("p1", ShadowCanaryObservation(EvaluationMode.SHADOW, "s1", True, 100, {"error_rate": 0.0}, 0.0, ("r1",)))
    lifecycle.begin_canary("p1")
    lifecycle.record_result("p1", _result(suite, "r2", EvaluationMode.CANARY))
    lifecycle.record_observation("p1", ShadowCanaryObservation(EvaluationMode.CANARY, "c1", True, 50, {"error_rate": 0.01}, 0.1, ("r2",)))
    return lifecycle, suite


def test_suite_and_result_digest_include_dimensions_and_identity() -> None:
    suite = _suite()
    result = _result(suite, "r1")
    altered = EvaluationSuite("quality", "v3", (EvaluationDimension("hardware", "cpu"), EvaluationDimension("split", "holdout")), ("accuracy", "latency_ms"))
    assert suite.digest != altered.digest
    assert result.as_dict()["dimensions"] == [d.as_dict() for d in suite.dimensions]
    assert result.digest


def test_result_rejects_dimension_or_metric_drift() -> None:
    suite = _suite()
    with pytest.raises(EvaluationLifecycleError):
        EvaluationResult("r", suite, "c", "b", EvaluationMode.OFFLINE, (suite.dimensions[0],), {"accuracy": 1, "latency_ms": 2}, True, 1)
    with pytest.raises(EvaluationLifecycleError):
        EvaluationResult("r", suite, "c", "b", EvaluationMode.OFFLINE, suite.dimensions, {"accuracy": 1, "other": 2}, True, 1)


def test_shadow_must_be_healthy_before_canary() -> None:
    suite = _suite()
    lifecycle = ProposalLifecycle()
    lifecycle.create("p", "candidate@sha", "baseline@sha", suite)
    lifecycle.submit("p")
    lifecycle.begin_evaluation("p")
    lifecycle.begin_shadow("p")
    lifecycle.record_observation("p", ShadowCanaryObservation(EvaluationMode.SHADOW, "s", False, 10, {"error": 1}, 0.0))
    with pytest.raises(EvaluationLifecycleError, match="healthy shadow"):
        lifecycle.begin_canary("p")


def test_promotion_evidence_requires_all_safety_dimensions_and_explicit_approval() -> None:
    lifecycle, _ = _lifecycle()
    evidence = lifecycle.build_promotion_evidence(
        "p1", gate_results={"quality": True, "latency": True}, replay_equivalent=True,
        holdout_passed=True, rollback_reference="route:baseline", provenance=("eval-run:r1",),
    )
    assert evidence.accepted is True
    assert lifecycle.approve("p1", evidence.digest).state is ProposalState.READY_FOR_PROMOTION
    with pytest.raises(EvaluationLifecycleError, match="attended"):
        lifecycle.promote("p1", evidence.digest)
    assert lifecycle.promote("p1", evidence.digest, attended=True).state is ProposalState.PROMOTED


def test_failed_gate_cannot_be_approved_and_evidence_is_stable() -> None:
    lifecycle, _ = _lifecycle()
    evidence = lifecycle.build_promotion_evidence(
        "p1", gate_results={"quality": False}, replay_equivalent=True,
        holdout_passed=True, rollback_reference="route:baseline", provenance=("eval-run:r1",),
    )
    assert evidence.accepted is False
    with pytest.raises(EvaluationLifecycleError, match="rejected"):
        lifecycle.approve("p1", evidence.digest)


def test_invalid_transition_and_immutable_result_id_fail_closed() -> None:
    suite = _suite()
    lifecycle = ProposalLifecycle()
    lifecycle.create("p", "candidate@sha", "baseline@sha", suite)
    with pytest.raises(EvaluationLifecycleError):
        lifecycle.begin_evaluation("p")
    lifecycle.submit("p")
    lifecycle.begin_evaluation("p")
    lifecycle.record_result("p", _result(suite, "r"))
    with pytest.raises(EvaluationLifecycleError, match="immutable"):
        lifecycle.record_result("p", replace(_result(suite, "r"), metrics={"accuracy": 0.5, "latency_ms": 12}))


def test_phase_result_modes_and_attended_rollback_are_explicit() -> None:
    lifecycle, suite = _lifecycle()
    with pytest.raises(EvaluationLifecycleError, match="canary mode"):
        lifecycle.record_result("p1", _result(suite, "wrong-mode", EvaluationMode.OFFLINE))
    evidence = lifecycle.build_promotion_evidence(
        "p1", gate_results={"quality": True}, replay_equivalent=True,
        holdout_passed=True, rollback_reference="route:baseline", provenance=("eval-run:r1",),
    )
    lifecycle.approve("p1", evidence.digest)
    lifecycle.promote("p1", evidence.digest, attended=True)
    with pytest.raises(EvaluationLifecycleError, match="attended"):
        lifecycle.rollback("p1")
    assert lifecycle.rollback("p1", attended=True).state is ProposalState.ROLLED_BACK
