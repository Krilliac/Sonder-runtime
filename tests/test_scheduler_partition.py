from __future__ import annotations

import pytest

from sonder_runtime.domain.scheduler_partition import (
    PartitionDescriptor,
    PartitionRouter,
    PartitionRoutingError,
)


def _partition(name, owner, *, capacity=10, revision=1, status="active"):
    return PartitionDescriptor(name, owner, capacity, revision, status)


def test_partition_descriptors_are_bounded_and_versioned():
    with pytest.raises(PartitionRoutingError, match="partition_id"):
        _partition("bad/id", "owner")
    with pytest.raises(PartitionRoutingError, match="capacity"):
        _partition("p1", "owner", capacity=0)
    with pytest.raises(PartitionRoutingError, match="status"):
        _partition("p1", "owner", status="unknown")


def test_router_routes_consistently_across_active_partitions_and_skips_draining():
    router = PartitionRouter([
        _partition("p1", "node-1"),
        _partition("p2", "node-2", capacity=20),
        _partition("p3", "node-3", status="draining"),
    ])
    first = router.route("session-42")
    assert first.partition_id in {"p1", "p2"}
    assert router.route("session-42") == first
    assert router.route("session-42").owner_id == first.owner_id
    with pytest.raises(PartitionRoutingError, match="no active"):
        PartitionRouter([_partition("p1", "node-1", status="paused")]).route("session-42")


def test_router_updates_require_monotonic_revision_and_preserve_route_bounds():
    router = PartitionRouter([_partition("p1", "node-1")])
    router.upsert(_partition("p1", "node-1", capacity=20, revision=2))
    assert router.partition("p1").capacity == 20
    with pytest.raises(PartitionRoutingError, match="revision"):
        router.upsert(_partition("p1", "node-2", revision=1))
    with pytest.raises(PartitionRoutingError, match="duplicate"):
        PartitionRouter([_partition("p1", "node-1"), _partition("p1", "node-2")])


def test_inventory_pages_are_stable_bounded_and_resumable():
    router = PartitionRouter([_partition("p%02d" % i, "node-%02d" % i) for i in range(5)])
    page = router.page(limit=2)
    assert tuple(item.partition_id for item in page.items) == ("p00", "p01")
    assert page.complete is False
    assert page.next_cursor == "p01"
    tail = router.page(after=page.next_cursor, limit=8)
    assert tuple(item.partition_id for item in tail.items) == ("p02", "p03", "p04")
    assert tail.complete is True
    with pytest.raises(PartitionRoutingError, match="limit"):
        router.page(limit=0)


def test_protocol_negotiation_is_explicit_and_does_not_promote_partitions():
    router = PartitionRouter([_partition("p1", "node-1")], protocol_version=2)
    assert router.negotiate(2).accepted
    rejected = router.negotiate(1)
    assert rejected.accepted is False
    assert rejected.reason == "protocol_version_mismatch"
    assert router.partition("p1").status == "active"
