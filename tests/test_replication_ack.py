from __future__ import annotations

import pytest

from sonder_runtime.domain.replication_ack import (
    ControlEvent,
    ReplicaAcknowledgement,
    ReplicaRole,
    ReplicationAcknowledgementPolicy,
    ReplicationMode,
    ReplicationReason,
    ReplicationState,
)


_DIGEST = "a" * 64


def _event(**changes) -> ControlEvent:
    values = {
        "cluster_id": "cluster-a",
        "event_id": "event-1",
        "source_node_id": "node-1",
        "owner_id": "owner-a",
        "owner_epoch": 7,
        "lease_id": "lease-7",
        "event_digest": _DIGEST,
    }
    values.update(changes)
    return ControlEvent(**values)


def _ack(event: ControlEvent, replica_id: str = "node-2", **changes) -> ReplicaAcknowledgement:
    values = {
        "acknowledgement_id": f"ack-{replica_id}",
        "cluster_id": event.cluster_id,
        "event_id": event.event_id,
        "source_node_id": event.source_node_id,
        "replica_id": replica_id,
        "role": ReplicaRole.DATA,
        "authorized": True,
        "reachable": True,
        "durable": True,
        "owner_id": event.owner_id,
        "owner_epoch": event.owner_epoch,
        "lease_id": event.lease_id,
        "event_digest": event.event_digest,
    }
    values.update(changes)
    return ReplicaAcknowledgement(**values)


def _pair(**changes) -> ReplicationAcknowledgementPolicy:
    values = {
        "mode": ReplicationMode.POOLED_PAIR,
        "local_node_id": "node-1",
        "authorized_data_replica_ids": frozenset({"node-2"}),
    }
    values.update(changes)
    return ReplicationAcknowledgementPolicy(**values)


def _quorum(**changes) -> ReplicationAcknowledgementPolicy:
    values = {
        "mode": ReplicationMode.REPLICATED_DATA_QUORUM,
        "local_node_id": "node-1",
        "authorized_data_replica_ids": frozenset({"node-2", "node-3", "node-4"}),
        "required_data_replicas": 2,
    }
    values.update(changes)
    return ReplicationAcknowledgementPolicy(**values)


def test_local_sqlite_is_reported_as_local_only_and_never_as_replication_ack():
    policy = ReplicationAcknowledgementPolicy(
        mode=ReplicationMode.LOCAL_SQLITE,
        local_node_id="node-1",
    )

    decision = policy.evaluate(_event(), (), local_durable=True)

    assert decision.state is ReplicationState.LOCAL_ONLY
    assert decision.local_durable is True
    assert decision.acknowledged is False
    assert decision.replicated is False
    assert decision.required_data_replicas == 0
    assert ReplicationReason.LOCAL_SQLITE_ONLY in decision.reason_codes


def test_local_sqlite_without_local_commit_is_unavailable():
    policy = ReplicationAcknowledgementPolicy(
        mode=ReplicationMode.LOCAL_SQLITE,
        local_node_id="node-1",
    )

    decision = policy.assess(_event(), local_commit_durable=False)

    assert decision.state is ReplicationState.UNAVAILABLE
    assert decision.local_durable is False
    assert ReplicationReason.LOCAL_DURABILITY_MISSING in decision.reason_codes


def test_two_pc_pair_accepts_one_distinct_authorized_durable_data_replica():
    event = _event()
    decision = _pair().evaluate(event, (_ack(event),), local_durable=True)

    assert decision.state is ReplicationState.ACKNOWLEDGED
    assert decision.acknowledged is True
    assert decision.replicated is True
    assert decision.data_replica_count == 1
    assert decision.replica_ids == ("node-2",)
    assert ReplicationReason.DATA_QUORUM_REACHED in decision.reason_codes


def test_missing_pair_evidence_is_unavailable_and_does_not_use_local_sqlite():
    event = _event()
    decision = _pair().evaluate(event, (), local_durable=True)

    assert decision.state is ReplicationState.UNAVAILABLE
    assert decision.acknowledged is False
    assert ReplicationReason.DATA_REPLICA_EVIDENCE_MISSING in decision.reason_codes


def test_arbitration_witness_never_counts_as_a_data_copy():
    event = _event()
    policy = _quorum(arbitration_witness_ids=frozenset({"witness-1"}))
    witness_ack = _ack(
        event,
        "witness-1",
        role=ReplicaRole.ARBITRATION_WITNESS,
    )

    decision = policy.evaluate(event, (witness_ack,), local_durable=True)

    assert decision.state is ReplicationState.UNAVAILABLE
    assert decision.data_replica_count == 0
    assert decision.acknowledged is False
    assert ReplicationReason.ARBITRATION_WITNESS_NOT_DATA in decision.reason_codes


def test_pair_ack_requires_policy_authorization_and_distinct_replica():
    event = _event()
    unauthorized = _ack(event, "node-9")
    same_node = _ack(event, "node-1")

    decision = _pair().evaluate(
        event,
        (unauthorized, same_node),
        local_durable=True,
    )

    assert decision.state is ReplicationState.UNAVAILABLE
    assert decision.data_replica_count == 0
    assert ReplicationReason.REPLICA_NOT_AUTHORIZED in decision.reason_codes
    assert ReplicationReason.REPLICA_NOT_DISTINCT in decision.reason_codes


def test_ack_is_bound_to_event_digest_epoch_and_lease():
    event = _event()
    mismatched = _ack(
        event,
        event_digest="b" * 64,
        owner_epoch=event.owner_epoch + 1,
        lease_id="lease-other",
    )

    decision = _pair().evaluate(event, (mismatched,), local_durable=True)

    assert decision.state is ReplicationState.UNAVAILABLE
    assert decision.data_replica_count == 0
    assert ReplicationReason.EVENT_DIGEST_MISMATCH in decision.reason_codes
    assert ReplicationReason.OWNER_EPOCH_MISMATCH in decision.reason_codes
    assert ReplicationReason.LEASE_MISMATCH in decision.reason_codes


def test_ack_requires_durable_reachable_copy_and_partitions_pause_progress():
    event = _event()
    unreachable = _ack(event, reachable=False)

    decision = _pair().evaluate(event, (unreachable,), local_durable=True)

    assert decision.state is ReplicationState.PAUSED
    assert decision.acknowledged is False
    assert ReplicationReason.REPLICA_UNREACHABLE in decision.reason_codes
    assert ReplicationReason.PARTITION_PREVENTS_ACK in decision.reason_codes


def test_missing_local_durability_pauses_even_when_replica_receipt_exists():
    event = _event()
    decision = _pair().evaluate(event, (_ack(event),), local_durable=False)

    assert decision.state is ReplicationState.PAUSED
    assert decision.acknowledged is False
    assert ReplicationReason.LOCAL_DURABILITY_MISSING in decision.reason_codes


def test_quorum_counts_distinct_data_replicas_and_ignores_duplicate_receipts():
    event = _event()
    first = _ack(event, "node-2")
    second = _ack(event, "node-3")
    decision = _quorum().evaluate(
        event,
        (first, first, second),
        local_durable=True,
    )

    assert decision.state is ReplicationState.ACKNOWLEDGED
    assert decision.data_replica_count == 2
    assert decision.replica_ids == ("node-2", "node-3")


def test_conflicting_receipts_for_one_replica_fail_closed():
    event = _event()
    good = _ack(event, "node-2")
    conflicting = _ack(event, "node-2", event_digest="c" * 64)

    decision = _quorum().evaluate(
        event,
        (good, conflicting, _ack(event, "node-3")),
        local_durable=True,
    )

    assert decision.state is ReplicationState.PAUSED
    assert decision.data_replica_count == 1
    assert decision.acknowledged is False
    assert ReplicationReason.CONFLICTING_REPLICA_EVIDENCE in decision.reason_codes


def test_quorum_shortfall_with_reachable_valid_receipt_is_paused():
    event = _event()
    decision = _quorum().evaluate(event, (_ack(event, "node-2"),), local_durable=True)

    assert decision.state is ReplicationState.PAUSED
    assert decision.data_replica_count == 1
    assert ReplicationReason.DATA_QUORUM_NOT_REACHED in decision.reason_codes


def test_global_partition_is_explicitly_paused_until_data_quorum_is_proven():
    event = _event()
    decision = _quorum().evaluate(
        event,
        (_ack(event, "node-2"),),
        local_durable=True,
        partitioned=True,
    )

    assert decision.state is ReplicationState.PAUSED
    assert ReplicationReason.PARTITION_PREVENTS_ACK in decision.reason_codes


def test_policy_rejects_invalid_replica_membership_and_quorum():
    with pytest.raises(ValueError, match="local node"):
        ReplicationAcknowledgementPolicy(
            mode=ReplicationMode.POOLED_PAIR,
            local_node_id="node-1",
            authorized_data_replica_ids=frozenset({"node-1"}),
        )
    with pytest.raises(ValueError, match="witness"):
        ReplicationAcknowledgementPolicy(
            mode=ReplicationMode.REPLICATED_DATA_QUORUM,
            local_node_id="node-1",
            authorized_data_replica_ids=frozenset({"node-2"}),
            arbitration_witness_ids=frozenset({"node-2"}),
            required_data_replicas=1,
        )
    with pytest.raises(ValueError, match="required_data_replicas"):
        ReplicationAcknowledgementPolicy(
            mode=ReplicationMode.REPLICATED_DATA_QUORUM,
            local_node_id="node-1",
            authorized_data_replica_ids=frozenset({"node-2"}),
            required_data_replicas=0,
        )


def test_event_and_acknowledgement_reject_unbounded_or_mutable_identity():
    with pytest.raises(ValueError, match="digest"):
        _event(event_digest="not-a-digest")
    with pytest.raises(ValueError, match="boolean"):
        _ack(_event(), authorized=1)


def test_acknowledgement_requires_event_identity_and_source_binding():
    event = _event()
    wrong_event = _ack(event, event_id="event-other")
    wrong_source = _ack(event, "node-9", source_node_id="node-other")
    decision = _pair().evaluate(event, (wrong_event, wrong_source), local_durable=True)

    assert decision.state is ReplicationState.UNAVAILABLE
    assert ReplicationReason.EVENT_ID_MISMATCH in decision.reason_codes
    assert ReplicationReason.SOURCE_NODE_MISMATCH in decision.reason_codes


def test_unreachable_receipt_without_global_flag_still_reports_partition_pause():
    event = _event()
    decision = _pair().evaluate(event, (_ack(event, reachable=False),), local_durable=True)

    assert decision.state is ReplicationState.PAUSED
    assert ReplicationReason.PARTITION_PREVENTS_ACK in decision.reason_codes


def test_data_quorum_requires_configured_data_replica_when_none_is_authorized():
    event = _event()
    policy = ReplicationAcknowledgementPolicy(
        mode=ReplicationMode.REPLICATED_DATA_QUORUM,
        local_node_id="node-1",
        authorized_data_replica_ids=frozenset(),
        required_data_replicas=2,
    )

    decision = policy.evaluate(event, (), local_durable=True)

    assert decision.state is ReplicationState.UNAVAILABLE
    assert ReplicationReason.DATA_REPLICA_NOT_CONFIGURED in decision.reason_codes


def test_witness_with_data_role_is_still_rejected_by_authorized_witness_membership():
    event = _event()
    policy = _pair(arbitration_witness_ids=frozenset({"node-2"}), authorized_data_replica_ids=frozenset({"node-3"}))
    witness = _ack(event, "node-2", role=ReplicaRole.DATA)

    decision = policy.evaluate(event, (witness,), local_durable=True)

    assert decision.state is ReplicationState.UNAVAILABLE
    assert ReplicationReason.ARBITRATION_WITNESS_NOT_DATA in decision.reason_codes


def test_event_digest_is_immutable_and_acknowledgement_is_frozen():
    event = _event()
    ack = _ack(event)
    with pytest.raises((AttributeError, TypeError)):
        event.event_digest = "b" * 64
    with pytest.raises((AttributeError, TypeError)):
        ack.durable = False
