import pytest

from sonder_runtime.application.promotion.gates import MeasuredPromotionGates
from sonder_runtime.domain.promotion.measured import (
    MeasuredEvidence, PromotionArea, PromotionInputError, PromotionPolicy,
)


def _evidence(**changes):
    values = dict(
        area=PromotionArea.MODELS, candidate="candidate@sha", baseline="base@sha",
        metrics={"quality": .95, "latency": -.02}, holdout_passed=True,
        provenance=("eval-suite@sha", "dataset@sha"), rollback_reference="release-42",
    )
    values.update(changes)
    return MeasuredEvidence(**values)


def _gates(**policy_changes):
    policy = PromotionPolicy(
        minimums={"quality": .90}, maximum_regressions={"latency": .05},
        required_provenance=("eval-suite@sha", "dataset@sha"), **policy_changes,
    )
    return MeasuredPromotionGates({PromotionArea.MODELS: policy})


def test_all_required_evidence_approves_with_digest_and_rollback():
    decision = _gates().evaluate(_evidence())
    assert decision.accepted
    assert decision.reason == "promotion_approved"
    assert decision.rollback_reference == "release-42"
    assert len(decision.evidence_digest) == 64


@pytest.mark.parametrize("change, failure", [
    ({"holdout_passed": False}, "holdout"),
    ({"provenance": ("eval-suite@sha",)}, "provenance:dataset@sha"),
    ({"rollback_reference": ""}, "rollback"),
    ({"metrics": {"quality": .89, "latency": -.02}}, "metric_below:quality"),
    ({"metrics": {"quality": .95, "latency": -.06}}, "regression:latency"),
])
def test_each_safety_gate_rejects(change, failure):
    decision = _gates().evaluate(_evidence(**change))
    assert not decision.accepted
    assert failure in decision.failed_gates


def test_missing_metric_and_unknown_area_are_rejected():
    missing = _gates().evaluate(_evidence(metrics={"latency": 0}))
    assert "metric_missing:quality" in missing.failed_gates
    unknown = MeasuredPromotionGates({}).evaluate(_evidence())
    assert (not unknown.accepted) and unknown.reason == "no_policy"


def test_digest_changes_when_measured_evidence_changes():
    first = _gates().evaluate(_evidence())
    second = _gates().evaluate(_evidence(metrics={"quality": .96, "latency": -.02}))
    assert first.evidence_digest != second.evidence_digest


def test_domain_rejects_nonfinite_metrics_and_nonboolean_holdout():
    with pytest.raises(PromotionInputError):
        _evidence(metrics={"quality": float("nan")})
    with pytest.raises(PromotionInputError):
        _evidence(holdout_passed=1)
