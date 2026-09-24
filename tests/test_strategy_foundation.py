from dataclasses import replace

import pytest

from sonder_runtime.application.strategy.controller import (
    StrategyController,
    StrategyState,
)
from sonder_runtime.domain.strategy.models import (
    EvidenceRef,
    FailureClass,
    FailureObservation,
    ProgressAssessment,
    ProgressMetric,
    ProgressVector,
    StrategyAction,
    StrategyAttempt,
    StrategyBudget,
    StrategyError,
    StrategySignature,
    StrategyUsage,
    assess_progress,
)

DIGEST = "a" * 64


def signature(**changes):
    return StrategySignature(**dict({
        "family": "patch", "objective_digest": DIGEST,
        "target_scope": ("src/main.py",), "hypothesis_digest": "b" * 64,
        "intended_change": "correct the lifetime", "verifier_target": "unit-tests",
    }, **changes))


def vector(value, *, scope=DIGEST, complete=True):
    return ProgressVector(scope, (ProgressMetric("failing_tests", value),), complete)


def attempt(number=1, **changes):
    return StrategyAttempt(**dict({
        "run_id": "run-1", "attempt_id": f"attempt-{number}",
        "signature": signature(), "outcome": "failed",
        "failure": FailureObservation(FailureClass.TEST_FAILURE),
        "progress_before": vector(2), "progress_after": vector(2),
        "usage": StrategyUsage(attempts=1, model_calls=1),
        "evidence": (EvidenceRef("verifier", "receipt-1", DIGEST),),
    }, **changes))


def test_semantic_identity_is_structured_and_stable():
    one = signature(target_scope=("src/b.py", "src/a.py"), intended_change="correct  the lifetime")
    two = signature(target_scope=("src/a.py", "src/b.py"))
    assert one.digest == two.digest
    assert signature(hypothesis_digest="c" * 64).digest != signature().digest
    assert signature(target_scope=("other.py",)).digest != signature().digest


@pytest.mark.parametrize("kwargs", [
    {"objective_digest": "not-a-digest"}, {"target_scope": ("src/a.py", "src/a.py")},
    {"family": "arbitrary-retry"}, {"intended_change": ""},
])
def test_invalid_strategy_identity_is_rejected(kwargs):
    with pytest.raises(StrategyError):
        signature(**kwargs)


@pytest.mark.parametrize("classification", [FailureClass.UNCERTAIN_SIDE_EFFECT, FailureClass.RECOVERY_REQUIRED])
def test_uncertain_effects_cannot_be_replay_safe(classification):
    failure = FailureObservation(classification)
    assert failure.requires_reconciliation
    assert not failure.replay_safe
    assert not failure.transport_retryable


def test_progress_requires_comparable_complete_evidence():
    assert assess_progress(vector(3), vector(2)) is ProgressAssessment.IMPROVED
    assert assess_progress(vector(2), vector(3)) is ProgressAssessment.REGRESSED
    assert assess_progress(vector(2), vector(2)) is ProgressAssessment.NEUTRAL
    assert assess_progress(vector(2), vector(1, complete=False)) is ProgressAssessment.INCOMPARABLE
    assert assess_progress(vector(2), vector(1, scope="b" * 64)) is ProgressAssessment.INCOMPARABLE


@pytest.mark.parametrize("value", [-1, True, float("inf"), float("nan")])
def test_invalid_progress_and_budget_numbers(value):
    with pytest.raises(StrategyError):
        ProgressMetric("errors", value)
    with pytest.raises(StrategyError):
        StrategyBudget(attempts=value)


def test_attempt_roundtrip_keeps_evidence_identity_and_rejects_unknown_fields():
    original = attempt()
    assert StrategyAttempt.from_dict(original.as_dict()) == original
    assert StrategyAttempt.from_dict(original.as_dict()).digest == original.digest
    with pytest.raises(StrategyError):
        StrategyAttempt.from_dict({**original.as_dict(), "raw_prompt": "unbounded"})
    with pytest.raises(StrategyError):
        replace(original, evidence=(EvidenceRef("verifier", "receipt-1", "b" * 64), original.evidence[0]))


def test_budget_consumption_is_exact_and_cannot_expand_parent():
    budget = StrategyBudget(attempts=2, tokens=100)
    assert budget.allows(StrategyUsage(attempts=2, tokens=100))
    assert not budget.allows(StrategyUsage(attempts=3))
    assert budget.remaining(StrategyUsage(attempts=1, tokens=40)).tokens == 60
    with pytest.raises(StrategyError):
        budget.reserve(StrategyBudget(attempts=3))


def test_host_safety_precedes_model_proposal_and_retry_budget():
    controller = StrategyController()
    state = StrategyState(
        objective_digest=DIGEST, failure=FailureObservation(FailureClass.UNCERTAIN_SIDE_EFFECT),
        budget=StrategyBudget(attempts=0),
        available_actions=(StrategyAction.RETRY_TRANSIENT, StrategyAction.REPAIR),
    )
    assert controller.decide(state).action is StrategyAction.RECONCILE


def test_identical_failed_strategy_is_not_selected_forever():
    controller = StrategyController()
    state = StrategyState(
        objective_digest=DIGEST, history=(attempt(1), attempt(2)),
        failure=FailureObservation(FailureClass.TEST_FAILURE),
        available_actions=(StrategyAction.REPAIR, StrategyAction.CRITIC),
    )
    assert controller.decide(state).action is StrategyAction.CRITIC
    assert controller.decide(replace(state, available_actions=(StrategyAction.REPAIR,))).action is StrategyAction.PAUSE


def test_genuine_progress_resets_semantic_stall():
    state = StrategyState(
        objective_digest=DIGEST,
        history=(attempt(1), attempt(2, progress_after=vector(1))),
        failure=FailureObservation(FailureClass.TEST_FAILURE),
        available_actions=(StrategyAction.REPAIR, StrategyAction.CRITIC),
    )
    assert StrategyController().decide(state).action is StrategyAction.REPAIR


def test_final_budgeted_success_still_reaches_completion_gate():
    state = StrategyState(
        objective_digest=DIGEST, history=(attempt(outcome="succeeded", failure=None),),
        usage=StrategyUsage(attempts=1), budget=StrategyBudget(attempts=1),
    )
    assert StrategyController().decide(state).reason == "attempt_succeeded_await_completion_gate"
    assert StrategyController().decide(replace(state, usage=StrategyUsage(attempts=2))).action is StrategyAction.FAIL
    assert StrategyController().decide(replace(state, unresolved_effects=True)).action is StrategyAction.RECONCILE
