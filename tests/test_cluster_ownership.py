from __future__ import annotations

import pytest

from sonder_runtime.domain.cluster_ownership import (
    ClusterOwnershipAuthority,
    OwnershipConflict,
    OwnershipError,
    TakeoverDenied,
    TakeoverProof,
)


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


def test_initial_acquire_is_scoped_and_epoch_fenced():
    clock = Clock()
    authority = ClusterOwnershipAuthority("cluster-a", clock=clock)
    lease = authority.acquire("job", "job-1", "node-1", lease_seconds=10)

    assert lease.cluster_id == "cluster-a"
    assert lease.resource_kind == "job"
    assert lease.resource_id == "job-1"
    assert lease.owner_id == "node-1"
    assert lease.epoch == 1
    assert authority.validate(lease).allowed

    with pytest.raises(OwnershipConflict, match="already owned"):
        authority.acquire("job", "job-1", "node-2", lease_seconds=10)

    clock.value = 111.0
    decision = authority.validate(lease)
    assert not decision.allowed
    assert decision.reason == "lease_expired"


def test_reacquire_after_expiry_increments_epoch_and_fences_old_lease():
    clock = Clock()
    authority = ClusterOwnershipAuthority("cluster-a", clock=clock)
    old = authority.acquire("session", "s-1", "node-1", lease_seconds=5)
    clock.value = 106.0
    new = authority.acquire("session", "s-1", "node-2", lease_seconds=5)

    assert new.epoch == 2
    assert not authority.validate(old).allowed
    assert authority.validate(old).reason == "stale_epoch"
    assert authority.validate(new).allowed


def test_renew_requires_current_token_and_never_resurrects_expired_lease():
    clock = Clock()
    authority = ClusterOwnershipAuthority("cluster-a", clock=clock)
    lease = authority.acquire("attempt", "a-1", "node-1", lease_seconds=5)
    renewed = authority.renew(lease, lease_seconds=20)
    assert renewed.epoch == lease.epoch
    assert renewed.token == lease.token
    assert renewed.expires_at == 120.0

    clock.value = 121.0
    with pytest.raises(OwnershipConflict, match="lease_expired"):
        authority.renew(renewed, lease_seconds=20)

    forged = renewed.__class__(
        renewed.cluster_id, renewed.resource_kind, renewed.resource_id,
        renewed.owner_id, renewed.epoch, "wrong-token", renewed.expires_at,
    )
    with pytest.raises(OwnershipConflict, match="stale_lease"):
        authority.renew(forged, lease_seconds=20)


def test_release_is_exact_and_allows_a_new_epoch():
    authority = ClusterOwnershipAuthority("cluster-a", clock=Clock())
    lease = authority.acquire("approval", "approval-1", "node-1", lease_seconds=10)
    assert authority.release(lease) is True
    assert authority.release(lease) is False
    replacement = authority.acquire("approval", "approval-1", "node-2", lease_seconds=10)
    assert replacement.epoch == 2


def test_takeover_requires_external_fence_and_replicated_ack():
    clock = Clock()
    authority = ClusterOwnershipAuthority("cluster-a", clock=clock)
    old = authority.acquire("memory_write", "m-1", "node-1", lease_seconds=30)

    def proof(**changes):
        values = dict(
            cluster_id="cluster-a", resource_kind="memory_write",
            resource_id="m-1", previous_owner_id="node-1",
            previous_epoch=old.epoch, fence_receipt="fence-1",
            data_ack_epoch=old.epoch, replica_ids=("node-1", "node-2"),
        )
        values.update(changes)
        return TakeoverProof(**values)

    with pytest.raises(TakeoverDenied, match="fence"):
        authority.takeover(proof(fence_receipt=""), new_owner_id="node-2", lease_seconds=10)
    with pytest.raises(TakeoverDenied, match="replica"):
        authority.takeover(proof(replica_ids=("node-1",)), new_owner_id="node-2", lease_seconds=10)
    with pytest.raises(TakeoverDenied, match="acknowledged"):
        authority.takeover(proof(data_ack_epoch=0), new_owner_id="node-2", lease_seconds=10)

    replacement = authority.takeover(proof(), new_owner_id="node-2", lease_seconds=10)
    assert replacement.epoch == old.epoch + 1
    assert replacement.owner_id == "node-2"
    assert not authority.validate(old).allowed
    assert authority.validate(old).reason == "stale_epoch"
    assert authority.validate(replacement).allowed


def test_takeover_rejects_mismatched_or_unbounded_proofs():
    clock = Clock()
    authority = ClusterOwnershipAuthority("cluster-a", clock=clock)
    old = authority.acquire("job", "job-1", "node-1", lease_seconds=30)
    with pytest.raises(OwnershipError, match="resource_kind"):
        TakeoverProof(
            "cluster-a", "unknown", "job-1", "node-1", old.epoch,
            "fence", old.epoch, ("node-1", "node-2"),
        )
    proof = TakeoverProof(
        "cluster-a", "job", "job-1", "node-1", old.epoch,
        "fence", old.epoch, ("node-1", "node-2"),
    )
    with pytest.raises(TakeoverDenied, match="does not match"):
        authority.takeover(proof, new_owner_id="node-3", lease_seconds=10, resource_id="other")
    with pytest.raises(OwnershipError, match="lease_seconds"):
        authority.takeover(proof, new_owner_id="node-2", lease_seconds=0)
