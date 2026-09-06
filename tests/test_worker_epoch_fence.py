from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from sonder_runtime.domain.worker_epoch_fence import (
    EffectKind,
    OwnerEpochObservation,
    WorkerEffectRequest,
    WorkerEffectReason,
    evaluate_worker_effect,
)


def _request(**changes) -> WorkerEffectRequest:
    values = {
        "cluster_id": "cluster-a",
        "resource_kind": "job",
        "resource_id": "job-1",
        "owner_id": "worker-a",
        "owner_epoch": 4,
        "lease_token_digest": "a" * 64,
        "effect": EffectKind.WRITE,
        "operation_id": "operation-1",
    }
    values.update(changes)
    return WorkerEffectRequest(**values)


def _observation(request: WorkerEffectRequest, **changes) -> OwnerEpochObservation:
    values = {
        "cluster_id": request.cluster_id,
        "resource_kind": request.resource_kind,
        "resource_id": request.resource_id,
        "owner_id": request.owner_id,
        "owner_epoch": request.owner_epoch,
        "lease_token_digest": request.lease_token_digest,
        "active": True,
    }
    values.update(changes)
    return OwnerEpochObservation(**values)


def test_matching_owner_epoch_and_token_allows_a_mutating_effect():
    request = _request()

    decision = evaluate_worker_effect(request, _observation(request))

    assert decision.allowed is True
    assert decision.reason_code == WorkerEffectReason.CURRENT_OWNER.value
    assert decision.observed_epoch == request.owner_epoch
    assert decision.as_dict() == {
        "allowed": True,
        "effect": "write",
        "reason_code": "current_owner",
        "expected_epoch": 4,
        "observed_epoch": 4,
        "operation_id": "operation-1",
    }


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"owner_epoch": 3},
            WorkerEffectReason.STALE_OWNER_EPOCH.value,
        ),
        (
            {"owner_epoch": 5},
            WorkerEffectReason.OWNER_EPOCH_MISMATCH.value,
        ),
        (
            {"owner_id": "worker-b"},
            WorkerEffectReason.OWNER_MISMATCH.value,
        ),
        (
            {"lease_token_digest": "b" * 64},
            WorkerEffectReason.STALE_OWNER_TOKEN.value,
        ),
    ],
)
def test_stale_or_different_owner_observation_blocks_mutation(changes, reason):
    request = _request()

    decision = evaluate_worker_effect(request, _observation(request, **changes))

    assert decision.allowed is False
    assert decision.reason_code == reason


def test_missing_or_inactive_owner_observation_blocks_mutation():
    request = _request()

    missing = evaluate_worker_effect(request, None)
    inactive = evaluate_worker_effect(request, _observation(request, active=False))

    assert missing.reason_code == WorkerEffectReason.OWNER_EVIDENCE_MISSING.value
    assert inactive.reason_code == WorkerEffectReason.OWNER_INACTIVE.value
    assert missing.allowed is False and inactive.allowed is False


@pytest.mark.parametrize(
    "field",
    ["cluster_id", "resource_kind", "resource_id"],
)
def test_observation_must_match_the_request_scope(field: str):
    request = _request()
    observation = _observation(request, **{field: "other"})

    decision = evaluate_worker_effect(request, observation)

    assert decision.allowed is False
    assert decision.reason_code == WorkerEffectReason.OWNERSHIP_SCOPE_MISMATCH.value


def test_reads_are_explicitly_unfenced_and_do_not_require_owner_evidence():
    request = _request(effect=EffectKind.READ)

    decision = evaluate_worker_effect(request, None)

    assert decision.allowed is True
    assert decision.reason_code == WorkerEffectReason.READ_UNFENCED.value
    assert decision.observed_epoch is None


def test_read_request_can_use_string_effect_and_decision_is_redacted():
    request = _request(effect="read")
    observation = _observation(request, lease_token_digest="b" * 64)

    decision = evaluate_worker_effect(request, observation)

    assert decision.allowed
    assert decision.effect is EffectKind.READ
    assert "lease_token_digest" not in decision.as_dict()


def test_contract_values_are_immutable_and_bounded():
    request = _request()
    observation = _observation(request)

    with pytest.raises((FrozenInstanceError, AttributeError)):
        request.owner_epoch = 5  # type: ignore[misc]
    with pytest.raises(ValueError, match="operation_id"):
        _request(operation_id="")
    with pytest.raises(ValueError, match="digest"):
        _request(lease_token_digest="not-a-digest")
    with pytest.raises(ValueError, match="owner_epoch"):
        _observation(request, owner_epoch=0)
    with pytest.raises(ValueError, match="effect"):
        _request(effect="unknown")


def test_non_effective_input_types_fail_closed_before_evaluation():
    request = _request()

    with pytest.raises(TypeError, match="WorkerEffectRequest"):
        evaluate_worker_effect(object(), _observation(request))
    with pytest.raises(TypeError, match="OwnerEpochObservation"):
        evaluate_worker_effect(request, object())
