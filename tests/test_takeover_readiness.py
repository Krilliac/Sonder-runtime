from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from sonder_runtime.domain.takeover_readiness import (
    DurableStateReplicationEvidence,
    IndependentQuorumDecision,
    OldOwnerFenceEvidence,
    TakeoverReadinessReason,
    TakeoverRequest,
    TakeoverTopology,
    evaluate_takeover_readiness,
)


def _request(**changes) -> TakeoverRequest:
    values = {
        "cluster_id": "cluster-a",
        "resource_kind": "session",
        "resource_id": "session-1",
        "previous_owner_id": "node-old",
        "previous_epoch": 7,
        "new_owner_id": "node-new",
        "topology": TakeoverTopology.TWO_NODE,
    }
    values.update(changes)
    return TakeoverRequest(**values)


def _fence(request: TakeoverRequest, **changes) -> OldOwnerFenceEvidence:
    values = {
        "cluster_id": request.cluster_id,
        "resource_kind": request.resource_kind,
        "resource_id": request.resource_id,
        "previous_owner_id": request.previous_owner_id,
        "previous_epoch": request.previous_epoch,
        "receipt_id": "fence-1",
        "authority_id": "fence-authority",
        "confirmed": True,
    }
    values.update(changes)
    return OldOwnerFenceEvidence(**values)


def _replication(request: TakeoverRequest, **changes) -> DurableStateReplicationEvidence:
    values = {
        "cluster_id": request.cluster_id,
        "resource_kind": request.resource_kind,
        "resource_id": request.resource_id,
        "previous_owner_id": request.previous_owner_id,
        "previous_epoch": request.previous_epoch,
        "receipt_id": "replication-1",
        "state_digest": "a" * 64,
        "acknowledged": True,
        "durable": True,
        "replica_ids": ("node-old", "node-new"),
    }
    values.update(changes)
    return DurableStateReplicationEvidence(**values)


def _quorum(request: TakeoverRequest, **changes) -> IndependentQuorumDecision:
    values = {
        "cluster_id": request.cluster_id,
        "resource_kind": request.resource_kind,
        "resource_id": request.resource_id,
        "previous_owner_id": request.previous_owner_id,
        "previous_epoch": request.previous_epoch,
        "decision_id": "quorum-1",
        "authority_id": "witness-authority",
        "witness_ids": ("witness-1",),
        "required_votes": 1,
        "received_votes": 1,
        "granted": True,
    }
    values.update(changes)
    return IndependentQuorumDecision(**values)


def test_readiness_is_ready_only_when_fence_replication_and_quorum_are_complete():
    request = _request()

    decision = evaluate_takeover_readiness(
        request,
        fence=_fence(request),
        replication=_replication(request),
        quorum=_quorum(request),
    )

    assert decision.ready is True
    assert decision.blocked is False
    assert decision.reason_codes == (TakeoverReadinessReason.READY.value,)
    assert decision.profile == TakeoverTopology.TWO_NODE.value
    assert decision.as_dict()["ready"] is True


def test_missing_evidence_is_blocked_with_one_reason_for_each_required_gate():
    decision = evaluate_takeover_readiness(_request())

    assert decision.ready is False
    assert decision.reason_codes == (
        TakeoverReadinessReason.OLD_OWNER_FENCE_MISSING.value,
        TakeoverReadinessReason.REPLICATION_EVIDENCE_MISSING.value,
        TakeoverReadinessReason.QUORUM_EVIDENCE_MISSING.value,
    )
    assert "fence" in decision.reason
    assert decision.as_dict()["ready"] is False


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "confirmed",
            False,
            TakeoverReadinessReason.OLD_OWNER_FENCE_UNCONFIRMED.value,
        ),
        (
            "authority_id",
            "node-old",
            TakeoverReadinessReason.OLD_OWNER_FENCE_NOT_INDEPENDENT.value,
        ),
    ],
)
def test_old_owner_fence_must_be_explicit_and_independent(
    field: str, value: object, reason: str
):
    request = _request()
    fence = _fence(request, **{field: value})

    decision = evaluate_takeover_readiness(
        request,
        fence=fence,
        replication=_replication(request),
        quorum=_quorum(request),
    )

    assert decision.blocked
    assert reason in decision.reason_codes


def test_fence_authority_cannot_be_a_data_replica():
    request = _request()

    decision = evaluate_takeover_readiness(
        request,
        fence=_fence(request),
        replication=_replication(request, replica_ids=("node-old", "fence-authority")),
        quorum=_quorum(request),
    )

    assert decision.blocked
    assert TakeoverReadinessReason.OLD_OWNER_FENCE_NOT_INDEPENDENT.value in decision.reason_codes


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"acknowledged": False},
            TakeoverReadinessReason.REPLICATION_NOT_ACKNOWLEDGED.value,
        ),
        (
            {"durable": False},
            TakeoverReadinessReason.REPLICATION_NOT_DURABLE.value,
        ),
        (
            {"replica_ids": ("node-old",)},
            TakeoverReadinessReason.REPLICATION_QUORUM_NOT_REACHED.value,
        ),
        (
            {"replica_ids": ("node-else", "node-new")},
            TakeoverReadinessReason.REPLICATION_SOURCE_MISSING.value,
        ),
    ],
)
def test_replication_must_be_acknowledged_durable_and_redundant(changes, reason):
    request = _request()

    decision = evaluate_takeover_readiness(
        request,
        fence=_fence(request),
        replication=_replication(request, **changes),
        quorum=_quorum(request),
    )

    assert decision.blocked
    assert reason in decision.reason_codes


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"granted": False},
            TakeoverReadinessReason.QUORUM_DENIED.value,
        ),
        (
            {"authority_id": "node-new"},
            TakeoverReadinessReason.QUORUM_NOT_INDEPENDENT.value,
        ),
        (
            {"required_votes": 2, "received_votes": 1},
            TakeoverReadinessReason.QUORUM_NOT_REACHED.value,
        ),
        (
            {"witness_ids": ("node-new",)},
            TakeoverReadinessReason.WITNESS_NOT_INDEPENDENT.value,
        ),
    ],
)
def test_quorum_must_be_granted_by_an_independent_witness(changes, reason):
    request = _request()

    decision = evaluate_takeover_readiness(
        request,
        fence=_fence(request),
        replication=_replication(request),
        quorum=_quorum(request, **changes),
    )

    assert decision.blocked
    assert reason in decision.reason_codes


def test_all_evidence_is_bound_to_the_requested_owner_epoch_and_scope():
    request = _request()

    decision = evaluate_takeover_readiness(
        request,
        fence=_fence(request, previous_epoch=8),
        replication=_replication(request, cluster_id="other-cluster"),
        quorum=_quorum(request, resource_id="other-resource"),
    )

    assert decision.blocked
    assert decision.reason_codes == (
        TakeoverReadinessReason.OLD_OWNER_FENCE_SCOPE_MISMATCH.value,
        TakeoverReadinessReason.REPLICATION_SCOPE_MISMATCH.value,
        TakeoverReadinessReason.QUORUM_SCOPE_MISMATCH.value,
    )


def test_single_host_and_two_node_limitations_are_visible_and_fail_closed_without_proof():
    single = evaluate_takeover_readiness(
        _request(topology=TakeoverTopology.SINGLE_HOST),
    )
    pair = evaluate_takeover_readiness(_request())

    assert single.profile == TakeoverTopology.SINGLE_HOST.value
    assert any("single-host" in item for item in single.limitations)
    assert any("external" in item for item in single.limitations)
    assert any("witness" in item for item in pair.limitations)
    assert single.blocked and pair.blocked


def test_evidence_objects_are_immutable_and_bounded():
    request = _request()
    evidence = _fence(request)

    with pytest.raises((FrozenInstanceError, AttributeError)):
        evidence.confirmed = False  # type: ignore[misc]
    with pytest.raises(ValueError, match="receipt_id"):
        _fence(request, receipt_id="")
    with pytest.raises(ValueError, match="replica_ids"):
        _replication(request, replica_ids=tuple(f"node-{i}" for i in range(65)))
    with pytest.raises(ValueError, match="witness_ids"):
        _quorum(request, witness_ids=tuple(f"witness-{i}" for i in range(33)))


def test_invalid_minimum_replica_bound_is_rejected_before_evaluation():
    with pytest.raises(ValueError, match="minimum_replicas"):
        evaluate_takeover_readiness(_request(), minimum_replicas=1)
    with pytest.raises(ValueError, match="minimum_replicas"):
        evaluate_takeover_readiness(_request(), minimum_replicas=65)
