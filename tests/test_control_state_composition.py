from __future__ import annotations

import pytest

from sonder_runtime.application.control_state import (
    ControlStateTakeoverAttempt,
    ExternalControlStateCoordinator,
)
from sonder_runtime.domain.cluster_availability import (
    ControlStateEvent,
    FenceReceipt,
    OwnershipScope,
    PartitionState,
    ReplicatedControlStateCapabilities,
    ReplicationAcknowledgement,
)
from sonder_runtime.domain.common.errors import DependencyUnavailable


def _capabilities(**changes) -> ReplicatedControlStateCapabilities:
    values = dict(
        provider_id="provider-1",
        data_replica_ids=("node-a", "node-b"),
        witness_ids=("witness-a",),
        durable_acknowledgements=True,
        external_fencing=True,
        partition_policy=PartitionState.SAFE,
    )
    values.update(changes)
    return ReplicatedControlStateCapabilities(**values)


def _event(**changes) -> ControlStateEvent:
    values = dict(
        event_id="event-1",
        cluster_id="cluster-a",
        resource_kind="job",
        resource_id="job-1",
        owner_id="node-a",
        owner_epoch=3,
        sequence=9,
        payload_digest="a" * 64,
    )
    values.update(changes)
    return ControlStateEvent(**values)


def _ack(event: ControlStateEvent, **changes) -> ReplicationAcknowledgement:
    values = dict(
        event_id=event.event_id,
        cluster_id=event.cluster_id,
        owner_epoch=event.owner_epoch,
        sequence=event.sequence,
        provider_id="provider-1",
        protocol_version=event.protocol_version,
        data_replica_ids=("node-a", "node-b"),
        witness_ids=("witness-a",),
        durable=True,
    )
    values.update(changes)
    return ReplicationAcknowledgement(**values)


def _fence(scope: OwnershipScope, **changes) -> FenceReceipt:
    values = dict(
        receipt_id="fence-1",
        cluster_id=scope.cluster_id,
        resource_kind=scope.resource_kind,
        resource_id=scope.resource_id,
        previous_owner_id=scope.owner_id,
        previous_owner_epoch=scope.epoch,
        provider_id="provider-1",
        protocol_version=1,
        partition_state=PartitionState.SAFE,
        external=True,
        accepted=True,
    )
    values.update(changes)
    return FenceReceipt(**values)


class _Provider:
    provider_id = "provider-1"
    protocol_version = 1

    def __init__(self, *, events=(), acknowledgement=None, fence=None):
        self.events = tuple(events)
        self.acknowledgement = acknowledgement
        self.fence_receipt = fence
        self.append_calls = 0
        self.fence_calls = 0
        self.read_calls = []

    def append(self, event):
        self.append_calls += 1
        return self.acknowledgement or _ack(event)

    def read(self, cluster_id, *, after_sequence=0, limit=128):
        self.read_calls.append((cluster_id, after_sequence, limit))
        return self.events

    def fence(self, ownership):
        self.fence_calls += 1
        return self.fence_receipt or _fence(ownership)


def _coordinator(provider: _Provider, **changes) -> ExternalControlStateCoordinator:
    values = {"minimum_data_replicas": 2}
    values.update(changes)
    return ExternalControlStateCoordinator(provider, _capabilities(), **values)


def test_prepare_takeover_collects_exact_receipts_and_only_returns_a_gate_decision():
    provider = _Provider()
    coordinator = _coordinator(provider)
    event = _event()
    scope = event.scope

    attempt = coordinator.prepare_takeover(scope, event, new_owner_id="node-b")

    assert isinstance(attempt, ControlStateTakeoverAttempt)
    assert attempt.decision.allowed is True
    assert attempt.decision.next_epoch == 4
    assert provider.append_calls == 1
    assert provider.fence_calls == 1
    assert attempt.as_dict()["decision"]["allowed"] is True


def test_existing_acknowledgement_is_revalidated_without_repeating_append():
    provider = _Provider()
    coordinator = _coordinator(provider)
    event = _event()
    ack = _ack(event)

    attempt = coordinator.prepare_takeover(
        event.scope,
        event,
        new_owner_id="node-b",
        acknowledgement=ack,
    )

    assert attempt.acknowledgement == ack
    assert provider.append_calls == 0
    assert provider.fence_calls == 1


@pytest.mark.parametrize(
    "ack_changes, reason",
    [
        ({"event_id": "other"}, "acknowledgement_mismatch"),
        ({"durable": False}, "durable_acknowledgement_required"),
        ({"data_replica_ids": ("node-a",)}, "insufficient_data_replicas"),
    ],
)
def test_append_rejects_invalid_provider_evidence(ack_changes, reason):
    event = _event()
    provider = _Provider(acknowledgement=_ack(event, **ack_changes))
    coordinator = _coordinator(provider)

    with pytest.raises(DependencyUnavailable, match=reason):
        coordinator.append(event)


def test_prepare_takeover_returns_blocked_decision_for_a_denied_or_ambiguous_fence():
    event = _event()
    provider = _Provider(fence=_fence(event.scope, accepted=False))
    attempt = _coordinator(provider).prepare_takeover(
        event.scope, event, new_owner_id="node-b"
    )
    assert attempt.decision.allowed is False
    assert attempt.decision.reason == "external_fence_receipt_required"

    provider = _Provider(fence=_fence(event.scope, partition_state=PartitionState.AMBIGUOUS))
    attempt = _coordinator(provider).prepare_takeover(
        event.scope, event, new_owner_id="node-b"
    )
    assert attempt.decision.allowed is False
    assert attempt.decision.reason == "ambiguous_partition"


def test_read_enforces_bounded_ordered_provider_pages():
    first = _event(sequence=10, event_id="event-10")
    second = _event(sequence=11, event_id="event-11")
    provider = _Provider(events=(first, second))
    coordinator = _coordinator(provider)

    assert coordinator.read("cluster-a", after_sequence=9, limit=2) == (first, second)
    assert provider.read_calls == [("cluster-a", 9, 2)]

    bad = _Provider(events=(second, first))
    with pytest.raises(DependencyUnavailable, match="not ordered"):
        _coordinator(bad).read("cluster-a", after_sequence=9)


def test_constructor_requires_matching_provider_identity_and_replicas():
    provider = _Provider()
    with pytest.raises(ValueError, match="protocol"):
        ExternalControlStateCoordinator(
            provider,
            _capabilities(protocol_version=2),
        )
    with pytest.raises(ValueError, match="fit"):
        ExternalControlStateCoordinator(
            provider,
            _capabilities(data_replica_ids=("node-a",)),
        )


def test_provider_exceptions_are_dependency_failures_and_scope_is_bound():
    class Broken(_Provider):
        def append(self, event):
            raise RuntimeError("private detail")

    event = _event()
    with pytest.raises(DependencyUnavailable, match="append failed"):
        _coordinator(Broken()).append(event)
    with pytest.raises(ValueError, match="scope"):
        _coordinator(_Provider()).prepare_takeover(
            OwnershipScope("cluster-a", "job", "other-job", "node-a", 3),
            event,
            new_owner_id="node-b",
        )
