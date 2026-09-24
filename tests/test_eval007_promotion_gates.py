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


def test_wilson_lower_bound_is_pinned_at_an_interior_rate() -> None:
    # p = 0.9 exercises the p(1 - p) term, which vanishes at p = 1.
    assert math.isclose(wilson_lower_bound(27, 30, 0.95), 0.77450, abs_tol=5e-6)


def test_duplicate_result_ids_cannot_inflate_samples() -> None:
    policy = DEFAULT_PROMOTION_GATE_POLICIES[PromotionKind.PROMPT]
    copies = [_result("r1", 10, 10)] * 3
    with pytest.raises(PromotionGateError, match="duplicate"):
        evaluate_promotion_gate(policy, results=copies, shadow=SHADOW, canary=CANARY)


def test_results_for_other_candidates_or_suites_are_not_pooled() -> None:
    policy = DEFAULT_PROMOTION_GATE_POLICIES[PromotionKind.PROMPT]
    other = EvaluationResult(
        "r2", SUITE, "someone-else", "baseline", EvaluationMode.OFFLINE, SUITE.dimensions,
        {"pass_rate": 1.0}, True, 20, replay_equivalent=True,
    )
    with pytest.raises(PromotionGateError, match="candidate"):
        evaluate_promotion_gate(policy, results=[_result("r1", 20, 20), other], shadow=SHADOW, canary=CANARY)
    suite_b = EvaluationSuite("prompt-quality", "v2", (EvaluationDimension("split", "holdout"),), ("pass_rate",))
    moved = EvaluationResult(
        "r3", suite_b, "candidate", "baseline", EvaluationMode.OFFLINE, suite_b.dimensions,
        {"pass_rate": 1.0}, True, 20, replay_equivalent=True,
    )
    with pytest.raises(PromotionGateError, match="suite"):
        evaluate_promotion_gate(policy, results=[_result("r1", 20, 20), moved], shadow=SHADOW, canary=CANARY)


def _proposal_through_canary(
    service: EvaluationApplicationService, proposal_id: str, offline, kind: PromotionKind | None = PromotionKind.PROMPT,
):
    service.register_suite(SUITE)
    service.create_proposal(proposal_id, "candidate", "baseline", SUITE, kind=kind)
    service.submit(proposal_id)
    service.begin_evaluation(proposal_id)
    for result in offline:
        service.record_result(proposal_id, result)
    service.begin_shadow(proposal_id)
    service.record_observation(proposal_id, SHADOW)
    service.begin_canary(proposal_id)
    service.record_observation(proposal_id, CANARY)


def _service() -> EvaluationApplicationService:
    return EvaluationApplicationService(corpus=BoundedEvaluationCorpusScanner([]), lifecycle=ProposalLifecycle())


def _gate(service: EvaluationApplicationService, proposal_id: str):
    return service.gated_promotion_evidence(
        proposal_id, baseline_pass_rate=None, case_regressions=0,
        holdout_passed=True, rollback_reference="baseline", provenance=("ci",),
    )


def test_service_recomputes_the_gate_from_lifecycle_recorded_results() -> None:
    service = _service()
    _proposal_through_canary(service, "p-thin", [_result("thin", 5, 5)])
    evidence = _gate(service, "p-thin")
    decision = service.promotion_gate_decision("p-thin", baseline_pass_rate=None, case_regressions=0)
    assert not evidence.accepted and not decision.passed
    assert decision.result_ids == ("thin",)
    assert evidence.provenance[-1] == f"promotion-gate:prompt:{decision.digest}"
    with pytest.raises(EvaluationLifecycleError, match="rejected"):
        service.approve("p-thin", evidence.digest)

    _proposal_through_canary(service, "p-ample", [_result("ample", 40, 40)])
    evidence = _gate(service, "p-ample")
    assert evidence.accepted
    assert service.approve("p-ample", evidence.digest).state is ProposalState.READY_FOR_PROMOTION
    assert service.promote("p-ample", evidence.digest, attended=True).state is ProposalState.PROMOTED


def test_gate_uses_the_kind_bound_at_proposal_creation() -> None:
    # 40/40 clears the PROMPT policy but not SELFMOD (60 samples, 0.95 bound).
    # The caller cannot choose a laxer kind at evidence time.
    service = _service()
    _proposal_through_canary(service, "p-self", [_result("ample", 40, 40)], kind=PromotionKind.SELFMOD)
    decision = service.promotion_gate_decision("p-self", baseline_pass_rate=None, case_regressions=0)
    assert decision.kind is PromotionKind.SELFMOD
    assert "gate_failed:sample_size" in decision.reason_codes
    assert not _gate(service, "p-self").accepted


def test_a_caller_constructed_decision_cannot_back_promotion() -> None:
    service = _service()
    _proposal_through_canary(service, "p-self", [_result("ample", 40, 40)], kind=PromotionKind.SELFMOD)
    forged = evaluate_promotion_gate(
        DEFAULT_PROMOTION_GATE_POLICIES[PromotionKind.PROMPT], results=[_result("ample", 40, 40)],
        shadow=SHADOW, canary=CANARY,
    )
    assert forged.passed
    with pytest.raises(TypeError):
        service.gated_promotion_evidence(  # type: ignore[call-arg]
            "p-self", forged, holdout_passed=True, rollback_reference="baseline", provenance=("ci",),
        )


def test_kind_bound_proposals_refuse_the_ungated_evidence_path() -> None:
    service = _service()
    lifecycle = ProposalLifecycle()
    service = EvaluationApplicationService(corpus=BoundedEvaluationCorpusScanner([]), lifecycle=lifecycle)
    _proposal_through_canary(service, "p1", [_result("ample", 40, 40)])
    with pytest.raises(PromotionGateError, match="gated"):
        service.promotion_evidence(
            "p1", gate_results={"quality": True}, replay_equivalent=True,
            holdout_passed=True, rollback_reference="baseline", provenance=("ci",),
        )
    bypass = lifecycle.build_promotion_evidence(
        "p1", gate_results={"quality": True}, replay_equivalent=True,
        holdout_passed=True, rollback_reference="baseline", provenance=("ci",),
    )
    assert bypass.accepted
    with pytest.raises(PromotionGateError, match="gated"):
        service.approve("p1", bypass.digest)


def test_legacy_unkinded_proposals_warn_on_the_ungated_path() -> None:
    service = _service()
    _proposal_through_canary(service, "legacy", [_result("ample", 40, 40)], kind=None)
    with pytest.warns(DeprecationWarning, match="ungated"):
        service.promotion_evidence(
            "legacy", gate_results={"quality": True}, replay_equivalent=True,
            holdout_passed=True, rollback_reference="baseline", provenance=("ci",),
        )
    with pytest.raises(PromotionGateError, match="kind"):
        _gate(service, "legacy")
