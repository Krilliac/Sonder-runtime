"""Membership capability evidence is renewed before it expires.

Admission requires unexpired capability evidence, and only the controller
reprobes membership workers. Probing only once a member is already stale left
every member inadmissible from TTL expiry until the next controller pass.
"""
from datetime import timedelta

from sonder_runtime.adapters.inference.ollama_pool import OllamaWorkerPool

from tests.test_inference_membership_controller import (
    LOCAL,
    REMOTE,
    Clock,
    Source,
    controller,
    signed_snapshot,
)


class _Monotonic:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


def test_controller_renews_member_capabilities_before_they_expire():
    wall, mono = Clock(), _Monotonic()
    probes = []

    def prober(origin):
        if origin == REMOTE:
            probes.append(origin)
        return {"models": ["code"]}

    # TTL 100 s with a 60 s controller interval: a pass at t=50 is the last one
    # before t=100, so it must renew; waiting for staleness leaves t=100..110 dark.
    pool = OllamaWorkerPool(LOCAL, (REMOTE,), allow_remote=True, clock=mono,
                            capability_ttl_seconds=100, capability_prober=prober)
    source = Source(signed_snapshot())
    control = controller(source, pool, wall)
    try:
        control.refresh(timeout_seconds=2)
        pool.refresh_capabilities()  # the local primary's own inventory
        assert probes == [REMOTE]
        assert pool.summary()["eligible_worker_count"] == 2

        wall.now += timedelta(seconds=50)
        mono.now += 50
        source.snapshot = signed_snapshot(generation=2, issued_at=wall.now)
        control.refresh(timeout_seconds=2)
        assert probes == [REMOTE, REMOTE]

        wall.now += timedelta(seconds=55)
        mono.now += 55
        pool.refresh_capabilities()  # renews only the stale local primary
        assert probes == [REMOTE, REMOTE]
        # Before the next scheduled pass (t=110), the member is still admissible.
        assert pool.summary()["eligible_worker_count"] == 2
    finally:
        assert control.close(timeout=2)


def test_controller_does_not_reprobe_members_far_from_expiry():
    wall, mono = Clock(), _Monotonic()
    probes = []
    pool = OllamaWorkerPool(LOCAL, (REMOTE,), allow_remote=True, clock=mono,
                            capability_ttl_seconds=300,
                            capability_prober=lambda origin: probes.append(origin) or {"models": ["code"]})
    source = Source(signed_snapshot())
    control = controller(source, pool, wall)
    try:
        control.refresh(timeout_seconds=2)
        # The first pass proves the member and renews the static loopback lane.
        assert sorted(probes) == sorted([LOCAL, REMOTE])
        wall.now += timedelta(seconds=30)
        mono.now += 30
        source.snapshot = signed_snapshot(generation=2, issued_at=wall.now)
        control.refresh(timeout_seconds=2)
        assert sorted(probes) == sorted([LOCAL, REMOTE])
    finally:
        assert control.close(timeout=2)
