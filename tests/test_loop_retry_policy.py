import pytest

from sonder_runtime.domain.loop_retry_policy import (
    BackoffMetadata,
    ReplayAction,
    RetryClass,
    SideEffectClass,
    classify_retry,
    retry_decision,
    side_effect_requirement,
)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"status": 503}, RetryClass.TRANSIENT),
        ({"status": 429}, RetryClass.THROTTLED),
        ({"failure_code": "connection-reset"}, RetryClass.TRANSIENT),
        ({"failure_code": "validation_failed"}, RetryClass.PERMANENT),
        ({"failure_code": "timeout", "outcome_known": False}, RetryClass.UNKNOWN_OUTCOME),
    ],
)
def test_classification_is_explicit_and_unknown_outcome_wins(kwargs, expected):
    assert classify_retry(**kwargs) is expected


def test_backoff_metadata_is_bounded_exponential_and_leaves_jitter_to_adapter():
    metadata = BackoffMetadata(base_seconds=2, maximum_seconds=5)
    assert metadata.strategy == "exponential_full_jitter"
    assert [metadata.cap_for_attempt(n) for n in (1, 2, 3, 4)] == [2, 4, 5, 5]


def test_unknown_idempotent_outcome_requires_same_key_and_reconciliation():
    decision = retry_decision(
        "timeout", outcome_known=False, effect=SideEffectClass.IDEMPOTENT,
        idempotency_key="turn-1/step-1/attempt-1",
    )
    assert decision.classification is RetryClass.UNKNOWN_OUTCOME
    assert decision.action is ReplayAction.RECONCILE_THEN_RETRY
    assert decision.side_effect.idempotency_key_required
    assert decision.side_effect.idempotency_key_present
    assert decision.side_effect.reconciliation_required


def test_read_only_transient_failure_can_retry_without_side_effect_proof():
    decision = retry_decision("timeout", effect=SideEffectClass.NONE)
    assert decision.action is ReplayAction.RETRY
    assert not decision.side_effect.idempotency_key_required
    assert not decision.side_effect.idempotency_key_present
    assert not decision.side_effect.reconciliation_required


def test_permanent_failure_and_attempt_limit_do_not_retry():
    assert retry_decision("invalid_request").action is ReplayAction.DO_NOT_RETRY
    assert retry_decision("timeout", attempt=3, max_attempts=3).action is ReplayAction.DO_NOT_RETRY


def test_non_idempotent_unknown_outcome_must_reconcile_before_replay():
    requirement = side_effect_requirement(
        SideEffectClass.NON_IDEMPOTENT, outcome_known=False,
    )
    assert requirement.reconciliation_required
    assert requirement.idempotency_key_required
    assert not requirement.idempotency_key_present
    assert retry_decision("timeout", effect=SideEffectClass.NON_IDEMPOTENT).action is ReplayAction.DO_NOT_RETRY
    with pytest.raises(ValueError):
        BackoffMetadata(base_seconds=3, maximum_seconds=2)
