from __future__ import annotations

import pytest
from types import SimpleNamespace

from sonder_runtime.domain.cluster_availability import (
    AvailabilityProfile,
    AvailabilityProfileError,
    ControlStateEvent,
    FenceReceipt,
    OwnershipScope,
    PartitionState,
    ReplicatedControlStateCapabilities,
    ReplicationAcknowledgement,
    assess_availability_profile,
    evaluate_takeover,
    validate_replication_acknowledgement,
)


def _provider(**changes):
    values = dict(
        provider_id="control-provider",
        protocol_version=1,
        data_replica_ids=("pc-a", "pc-b"),
        witness_ids=("witness",),
        durable_acknowledgements=True,
        external_fencing=True,
        partition_policy=PartitionState.SAFE,
    )
    values.update(changes)
    return ReplicatedControlStateCapabilities(**values)


def _event(*, owner="pc-a", epoch=4, sequence=9, **changes):
    values = dict(
        event_id="event-9",
        cluster_id="cluster-a",
        resource_kind="session",
        resource_id="session-1",
        owner_id=owner,
        owner_epoch=epoch,
        sequence=sequence,
        payload_digest="a" * 64,
        protocol_version=1,
    )
    values.update(changes)
    return ControlStateEvent(**values)


def _ack(event, **changes):
    values = dict(
        event_id=event.event_id,
        cluster_id=event.cluster_id,
        owner_epoch=event.owner_epoch,
        sequence=event.sequence,
        provider_id="control-provider",
        protocol_version=event.protocol_version,
        data_replica_ids=("pc-a", "pc-b"),
        witness_ids=("witness",),
        durable=True,
    )
    values.update(changes)
    return ReplicationAcknowledgement(**values)


def _fence(scope, **changes):
    values = dict(
        receipt_id="fence-4",
        cluster_id=scope.cluster_id,
        resource_kind=scope.resource_kind,
        resource_id=scope.resource_id,
        previous_owner_id=scope.owner_id,
        previous_owner_epoch=scope.epoch,
        provider_id="control-provider",
        protocol_version=1,
        partition_state=PartitionState.SAFE,
        external=True,
        accepted=True,
    )
    values.update(changes)
    return FenceReceipt(**values)


def test_single_pc_is_local_sqlite_and_has_no_takeover_path():
    status = assess_availability_profile(
        "single-pc", ("pc-a",), local_node_id="pc-a"
    )

    assert status.profile is AvailabilityProfile.SINGLE_PC
    assert status.valid
    assert status.control_state_scope == "local-sqlite"
    assert status.resource_pooling is False
    assert status.automatic_takeover_available is False
    assert status.takeover_mode == "disabled"
    assert "SQLite" in " ".join(status.limits)
    payload = status.as_dict()
    assert payload["capabilities"]["automatic_takeover"]["available"] is False
    assert payload["capabilities"]["acknowledged_state_replication"]["available"] is False


def test_ownership_scope_adapts_the_existing_epoch_lease_shape():
    scope = OwnershipScope.from_lease(
        SimpleNamespace(
            cluster_id="cluster-a",
            resource_kind="job",
            resource_id="job-1",
            owner_id="pc-a",
            epoch=7,
        )
    )

    assert scope.owner_epoch == 7
    assert scope == OwnershipScope("cluster-a", "job", "job-1", "pc-a", 7)


def test_legacy_profile_names_map_to_explicit_pc_profiles():
    single = assess_availability_profile(
        "single-host", ("pc-a",), local_node_id="pc-a"
    )
    pair = assess_availability_profile(
        "pooled-pair", ("pc-a", "pc-b"), local_node_id="pc-a"
    )

    assert single.profile is AvailabilityProfile.SINGLE_PC
    assert pair.profile is AvailabilityProfile.TWO_PC
    assert pair.resource_pooling
    assert pair.control_state_scope == "per-node-local-sqlite"
    assert pair.automatic_takeover_available is False


@pytest.mark.parametrize(
    "profile,members,local,match",
    [
        ("single-pc", ("pc-a", "pc-b"), "pc-a", "exactly one"),
        ("two-pc", ("pc-a",), "pc-a", "exactly two"),
        ("two-pc", ("pc-a", "pc-b", "pc-c"), "pc-a", "exactly two"),
        ("two-pc", ("pc-a", "pc-b"), "pc-x", "local_node_id"),
    ],
)
def test_profiles_reject_ambiguous_membership(profile, members, local, match):
    with pytest.raises(AvailabilityProfileError, match=match):
        assess_availability_profile(profile, members, local_node_id=local)


def test_single_pc_rejects_an_external_provider_to_keep_sqlite_local():
    with pytest.raises(AvailabilityProfileError, match="SQLite"):
        assess_availability_profile(
            AvailabilityProfile.SINGLE_PC,
            ("pc-a",),
            local_node_id="pc-a",
            provider=_provider(data_replica_ids=("pc-a", "pc-b")),
        )


def test_two_pc_provider_is_only_a_takeover_prerequisite_not_a_ha_claim():
    status = assess_availability_profile(
        AvailabilityProfile.TWO_PC,
        ("pc-a", "pc-b"),
        local_node_id="pc-a",
        preferred_primary="pc-b",
        provider=_provider(),
    )

    assert status.valid
    assert status.provider_contract_valid
    assert status.control_state_scope == "per-node-local-sqlite"
    assert status.automatic_takeover_available is False
    assert status.takeover_mode == "external-provider-proof-required"
    assert "does not claim" in " ".join(status.limits)


def test_provider_protocol_mismatch_is_reported_and_cannot_enable_takeover():
    status = assess_availability_profile(
        "two-pc",
        ("pc-a", "pc-b"),
        local_node_id="pc-a",
        provider=_provider(protocol_version=2),
    )

    assert status.valid
    assert status.provider_contract_valid is False
    assert status.takeover_mode == "protocol-version-mismatch"
    assert "protocol" in " ".join(status.reasons)


def test_acknowledgement_rule_counts_data_replicas_and_excludes_witnesses():
    event = _event()
    provider = _provider()

    accepted = validate_replication_acknowledgement(
        event, _ack(event), provider
    )
    assert accepted.accepted
    assert accepted.data_replica_count == 2

    witness_only = validate_replication_acknowledgement(
        event,
        _ack(event, data_replica_ids=("pc-a",), witness_ids=("pc-b", "witness")),
        provider,
    )
    assert witness_only.accepted is False
    assert witness_only.reason == "insufficient_data_replicas"


def test_takeover_requires_matching_fence_ack_and_a_safe_partition():
    event = _event()
    scope = OwnershipScope(
        cluster_id=event.cluster_id,
        resource_kind=event.resource_kind,
        resource_id=event.resource_id,
        owner_id=event.owner_id,
        epoch=event.owner_epoch,
    )
    decision = evaluate_takeover(
        scope,
        new_owner_id="pc-b",
        event=event,
        acknowledgement=_ack(event),
        fence_receipt=_fence(scope),
        provider=_provider(),
    )

    assert decision.allowed
    assert decision.reason == "takeover-proof-satisfied"
    assert decision.next_epoch == event.owner_epoch + 1


@pytest.mark.parametrize(
    "fence_changes,ack_changes,expected",
    [
        ({"partition_state": PartitionState.AMBIGUOUS}, {}, "ambiguous_partition"),
        ({"partition_state": PartitionState.MINORITY}, {}, "minority_partition"),
        ({}, {"protocol_version": 2}, "protocol_version_mismatch"),
        ({}, {"owner_epoch": 3}, "acknowledgement_mismatch"),
    ],
)
def test_takeover_fails_closed_for_partitions_stale_data_and_versions(
    fence_changes, ack_changes, expected
):
    event = _event()
    scope = OwnershipScope(
        event.cluster_id,
        event.resource_kind,
        event.resource_id,
        event.owner_id,
        event.owner_epoch,
    )
    decision = evaluate_takeover(
        scope,
        new_owner_id="pc-b",
        event=event,
        acknowledgement=_ack(event, **ack_changes),
        fence_receipt=_fence(scope, **fence_changes),
        provider=_provider(),
    )

    assert decision.allowed is False
    assert decision.reason == expected


def test_takeover_rejects_witness_only_ack_even_with_a_fence():
    event = _event()
    scope = OwnershipScope(
        event.cluster_id,
        event.resource_kind,
        event.resource_id,
        event.owner_id,
        event.owner_epoch,
    )
    decision = evaluate_takeover(
        scope,
        new_owner_id="pc-b",
        event=event,
        acknowledgement=_ack(
            event, data_replica_ids=("pc-a",), witness_ids=("pc-b", "witness")
        ),
        fence_receipt=_fence(scope),
        provider=_provider(),
    )

    assert decision.allowed is False
    assert decision.reason == "insufficient_data_replicas"
