"""EVAL-007: per-kind promotion thresholds with confidence requirements.

Gates are pure functions over recorded counts; no model or live traffic is used.
"""
from __future__ import annotations

import math

import pytest

from sonder_runtime.adapters.evaluation_corpus import BoundedEvaluationCorpusScanner
from sonder_runtime.application.evaluation.promotion_gates import (
    DEFAULT_PROMOTION_GATE_POLICIES,
    PromotionGateError,
    PromotionGatePolicy,
    PromotionKind,
    evaluate_promotion_gate,
    validate_policy_table,
    wilson_lower_bound,
)
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
from sonder_runtime.application.evaluation.service import EvaluationApplicationService


SUITE = EvaluationSuite("prompt-quality", "v1", (EvaluationDimension("split", "holdout"),), ("pass_rate",))
SHADOW = ShadowCanaryObservation(EvaluationMode.SHADOW, "s1", True, 20, {"error": 0}, 0)
CANARY = ShadowCanaryObservation(EvaluationMode.CANARY, "c1", True, 20, {"error": 0}, 0.05)


def _result(result_id: str, passed: int, total: int, *, replay: bool | None = True,
            mode: EvaluationMode = EvaluationMode.OFFLINE) -> EvaluationResult:
    return EvaluationResult(
        result_id, SUITE, "candidate", "baseline", mode, SUITE.dimensions,
        {"pass_rate": passed / total}, passed == total, total,
        replay_equivalent=replay, provenance=("test",),
    )


def test_wilson_lower_bound_distinguishes_evidence_volume() -> None:
    small = wilson_lower_bound(3, 3, 0.95)
    large = wilson_lower_bound(300, 300, 0.95)
    assert small < 0.6 < 0.98 < large <= 1.0
    z = 1.6448536269514722
    assert math.isclose(wilson_lower_bound(10, 10, 0.95), 10 / (10 + z * z), rel_tol=1e-9)
    assert wilson_lower_bound(0, 10, 0.95) == 0.0
    with pytest.raises(PromotionGateError):
        wilson_lower_bound(11, 10, 0.95)
    with pytest.raises(PromotionGateError):
        wilson_lower_bound(1, 1, 1.0)


def test_default_table_covers_every_promotion_kind_with_attainable_confidence() -> None:
    assert set(DEFAULT_PROMOTION_GATE_POLICIES) == set(PromotionKind)
    assert {kind.value for kind in PromotionKind} == {
        "runtime", "prompt", "skill", "route", "model", "memory", "selfmod",
    }
    validate_policy_table(DEFAULT_PROMOTION_GATE_POLICIES)
    for kind, policy in DEFAULT_PROMOTION_GATE_POLICIES.items():
        assert policy.kind is kind
        assert policy.require_replay_equivalence and policy.require_shadow and policy.require_canary
        perfect = wilson_lower_bound(policy.min_samples, policy.min_samples, policy.confidence)
        assert perfect >= policy.min_pass_rate_lower_bound, kind
    selfmod = DEFAULT_PROMOTION_GATE_POLICIES[PromotionKind.SELFMOD]
    assert selfmod.min_pass_rate == 1.0 and selfmod.max_case_regressions == 0


def test_perfect_but_tiny_run_fails_on_sample_size_and_confidence() -> None:
    policy = DEFAULT_PROMOTION_GATE_POLICIES[PromotionKind.PROMPT]
    decision = evaluate_promotion_gate(policy, results=[_result("r1", 3, 3)], shadow=SHADOW, canary=CANARY)
    assert decision.pass_rate == 1.0
    assert not decision.passed
    assert decision.reason_codes == ("gate_failed:confidence_lower_bound", "gate_failed:sample_size")


def test_pooled_sufficient_evidence_passes_every_named_gate() -> None:
    policy = DEFAULT_PROMOTION_GATE_POLICIES[PromotionKind.PROMPT]
    results = [_result("r1", 20, 20), _result("r2", 19, 20)]
    decision = evaluate_promotion_gate(policy, results=results, baseline_pass_rate=0.97, shadow=SHADOW, canary=CANARY)
    assert (decision.samples, decision.successes) == (40, 39)
    assert decision.passed, decision.reason_codes
    assert set(decision.gate_results) == {
        "canary", "case_regressions", "confidence_lower_bound", "pass_rate",
        "pass_rate_drop", "replay_equivalence", "sample_size", "shadow",
    }
    again = evaluate_promotion_gate(policy, results=list(reversed(results)), baseline_pass_rate=0.97, shadow=SHADOW, canary=CANARY)
    assert again.digest == decision.digest


def test_each_policy_requirement_fails_closed() -> None:
    policy = DEFAULT_PROMOTION_GATE_POLICIES[PromotionKind.PROMPT]
    good = [_result("r1", 40, 40)]

    def reasons(**kwargs):
        arguments = {"results": good, "shadow": SHADOW, "canary": CANARY, **kwargs}
        return evaluate_promotion_gate(policy, **arguments).reason_codes

    assert reasons(case_regressions=1) == ("gate_failed:case_regressions",)
    assert reasons(results=[_result("r1", 37, 40)], baseline_pass_rate=1.0) == (
        "gate_failed:pass_rate_drop",
    )
    assert reasons(results=[_result("r1", 40, 40, replay=None)]) == ("gate_failed:replay_equivalence",)
    assert reasons(canary=None) == ("gate_failed:canary",)
    unhealthy = ShadowCanaryObservation(EvaluationMode.SHADOW, "s2", False, 5, {"error": 1}, 0)
    assert reasons(shadow=unhealthy) == ("gate_failed:shadow",)
    thin_canary = ShadowCanaryObservation(EvaluationMode.CANARY, "c2", True, 1, {"error": 0}, 0.01)
    strict = PromotionGatePolicy(PromotionKind.PROMPT, 30, 0.9, 0.8, 0.95, min_canary_samples=10)
    assert evaluate_promotion_gate(strict, results=good, shadow=SHADOW, canary=thin_canary).reason_codes == (
        "gate_failed:canary",
    )
    only_canary = [_result("c", 40, 40, mode=EvaluationMode.CANARY)]
    assert reasons(results=only_canary)[0] == "no_offline_results"


def test_unmeasurable_evidence_is_refused_rather_than_rounded() -> None:
    policy = DEFAULT_PROMOTION_GATE_POLICIES[PromotionKind.MODEL]
    fractional = EvaluationResult(
        "r1", SUITE, "candidate", "baseline", EvaluationMode.OFFLINE, SUITE.dimensions,
        {"pass_rate": 0.333}, False, 10, replay_equivalent=True,
    )
    with pytest.raises(PromotionGateError, match="integral"):
        evaluate_promotion_gate(policy, results=[fractional])
    other_suite = EvaluationSuite("s", "v1", (EvaluationDimension("split", "holdout"),), ("accuracy",))
    no_rate = EvaluationResult(
        "r2", other_suite, "candidate", "baseline", EvaluationMode.OFFLINE, other_suite.dimensions,
        {"accuracy": 1.0}, True, 10,
    )
    with pytest.raises(PromotionGateError, match="pass_rate"):
        evaluate_promotion_gate(policy, results=[no_rate])


def test_policy_and_table_validation() -> None:
    with pytest.raises(PromotionGateError, match="lower bound"):
        PromotionGatePolicy(PromotionKind.ROUTE, 10, 0.8, 0.9, 0.95)
    with pytest.raises(PromotionGateError, match="shadow"):
        PromotionGatePolicy(PromotionKind.ROUTE, 10, 0.9, 0.8, 0.95, require_shadow=False)
    with pytest.raises(PromotionGateError, match="confidence"):
        PromotionGatePolicy(PromotionKind.ROUTE, 10, 0.9, 0.8, 0.3)
    partial = {k: v for k, v in DEFAULT_PROMOTION_GATE_POLICIES.items() if k is not PromotionKind.MEMORY}
    with pytest.raises(PromotionGateError, match="every promotion kind"):
        validate_policy_table(partial)
    mislabelled = dict(DEFAULT_PROMOTION_GATE_POLICIES)
    mislabelled[PromotionKind.MEMORY] = DEFAULT_PROMOTION_GATE_POLICIES[PromotionKind.ROUTE]
    with pytest.raises(PromotionGateError, match="mislabelled"):
        validate_policy_table(mislabelled)
    with pytest.raises(PromotionGateError):
        EvaluationApplicationService(
            corpus=BoundedEvaluationCorpusScanner([]), lifecycle=ProposalLifecycle(), gate_policies=partial,
        )


def _proposal_through_canary(service: EvaluationApplicationService, proposal_id: str, offline):
    service.register_suite(SUITE)
    service.create_proposal(proposal_id, "candidate", "baseline", SUITE)
    service.submit(proposal_id)
    service.begin_evaluation(proposal_id)
    for result in offline:
        service.record_result(proposal_id, result)
    service.begin_shadow(proposal_id)
    service.record_observation(proposal_id, SHADOW)
    service.begin_canary(proposal_id)
    service.record_observation(proposal_id, CANARY)


def test_service_binds_the_mechanical_decision_into_promotion_approval() -> None:
    service = EvaluationApplicationService(corpus=BoundedEvaluationCorpusScanner([]), lifecycle=ProposalLifecycle())

    thin = [_result("thin", 5, 5)]
    _proposal_through_canary(service, "p-thin", thin)
    refused = service.evaluate_promotion_gate(PromotionKind.PROMPT, results=thin, shadow=SHADOW, canary=CANARY)
    evidence = service.gated_promotion_evidence(
        "p-thin", refused, holdout_passed=True, rollback_reference="baseline", provenance=("ci",),
    )
    assert not evidence.accepted
    assert evidence.provenance[-1] == f"promotion-gate:prompt:{refused.digest}"
    with pytest.raises(EvaluationLifecycleError, match="rejected"):
        service.approve("p-thin", evidence.digest)

    ample = [_result("ample", 40, 40)]
    _proposal_through_canary(service, "p-ample", ample)
    accepted = service.evaluate_promotion_gate(PromotionKind.PROMPT, results=ample, shadow=SHADOW, canary=CANARY)
    evidence = service.gated_promotion_evidence(
        "p-ample", accepted, holdout_passed=True, rollback_reference="baseline", provenance=("ci",),
    )
    assert evidence.accepted
    assert service.approve("p-ample", evidence.digest).state is ProposalState.READY_FOR_PROMOTION
    assert service.promote("p-ample", evidence.digest, attended=True).state is ProposalState.PROMOTED
