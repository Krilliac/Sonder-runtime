from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event, Lock

from tests.test_compute_index import inventory, request, NOW


def test_shared_coordinator_singleflight_bounds_all_callers_and_exhausts_inventory():
    from sonder_runtime.application.compute_fabric.coordinator import ComputeRefreshCoordinator
    registry, snapshots = inventory(64, homogeneous=True)
    gate, entered = Event(), Event()
    lock = Lock()
    calls, active, peak = [], 0, 0
    class Source:
        def snapshot(self, node, *, now):
            nonlocal active, peak
            with lock:
                calls.append(node.node_id)
                active += 1
                peak = max(peak, active)
                if active == 8:
                    entered.set()
            assert gate.wait(5)
            with lock:
                active -= 1
            return next(item for item in snapshots if item.node.node_id == node.node_id)
    coordinator = ComputeRefreshCoordinator(registry, Source(), now=lambda: NOW,
                                             refresh_after=timedelta(seconds=10))
    # Fix all three callers to the same observation cohort before any probe completes.
    cohort = Barrier(3)
    refresh_nodes = coordinator._refresh_nodes
    def together(*args):
        cohort.wait(timeout=3)
        return refresh_nodes(*args)
    coordinator._refresh_nodes = together
    # Force first refresh without changing worker timestamps or manufacturing a receipt.
    with ThreadPoolExecutor(max_workers=3) as callers:
        futures = [callers.submit(coordinator.refresh, request(), force=True) for _ in range(3)]
        try:
            assert entered.wait(3)
            assert coordinator.state()["submitted"] <= 8
        finally:
            gate.set()
        for future in futures:
            future.result(timeout=10)
    coordinator._refresh_nodes = refresh_nodes
    assert peak <= 8
    assert len(calls) == 63
    assert set(calls) == {f"node-{i:03}" for i in range(1, 64)}
    # Normal warm callers share fresh evidence and perform no extra network I/O.
    before = len(calls)
    coordinator.refresh(request())
    assert len(calls) == before
    coordinator.close()
    assert coordinator.state()["submitted"] == 0


def test_refresh_prunes_only_configured_impossibility_and_local_consent():
    from sonder_runtime.application.compute_fabric.coordinator import ComputeRefreshCoordinator
    registry, snapshots = inventory(64)
    calls = []
    class Source:
        def snapshot(self, node, *, now):
            calls.append(node.node_id)
            return snapshots[int(node.node_id[-3:])]
    coordinator = ComputeRefreshCoordinator(registry, Source(), now=lambda: NOW,
                                             refresh_after=timedelta(seconds=10))
    coordinator.refresh(request(required_model="unobserved-model"), force=True)
    assert calls == ["node-001"]
    coordinator.refresh(request(allow_remote=False), force=True)
    assert calls == ["node-001"]
    coordinator.close()


def test_process_admission_is_shared_and_shutdown_does_not_claim_socket_cleanup():
    from sonder_runtime.application.compute_fabric.coordinator import ComputeRefreshCoordinator
    registry, snapshots = inventory(16, homogeneous=True)
    other, _ = inventory(16, homogeneous=True)
    release, saturated = Event(), Event()
    lock = Lock()
    active = peak = 0
    class Source:
        def snapshot(self, node, *, now):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 8:
                    saturated.set()
            assert release.wait(5)
            with lock:
                active -= 1
            return next(item for item in snapshots if item.node.node_id == node.node_id)
    coordinators = [ComputeRefreshCoordinator(item, Source(), now=lambda: NOW,
                    refresh_after=timedelta(seconds=10)) for item in (registry, other)]
    with ThreadPoolExecutor(max_workers=2) as callers:
        tasks = [callers.submit(item.refresh, request(), force=True) for item in coordinators]
        try:
            assert saturated.wait(3)
            import pytest
            owner = next(item for item in coordinators if item.state()["submitted"])
            with pytest.raises(TimeoutError, match="socket cleanup"):
                owner.close(timeout=0)
            assert owner.state()["submitted"] > 0
        finally:
            release.set()
        for future in tasks:
            try:
                future.result(timeout=10)
            except RuntimeError as error:
                assert "closed" in str(error)
    for item in coordinators:
        item.close()
    assert peak <= 8
