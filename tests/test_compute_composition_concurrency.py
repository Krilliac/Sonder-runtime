"""Concurrent first use must publish one compute graph per application."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
import pytest
from sonder_runtime.bootstrap import app as bootstrap
from sonder_runtime.domain.compute_fabric import NodeSnapshot
from sonder_runtime.platform.config import SonderConfig


@pytest.mark.parametrize("component,accessor", [
    ("ComputeNodeRegistry", "compute_registry"),
    ("ComputeFabricService", "compute_service"),
    ("LocalComputeSnapshotSource", "compute_snapshot"),
])
def test_concurrent_first_use_constructs_component_once(monkeypatch, component, accessor):
    from sonder_runtime.adapters.compute_fabric import local_snapshot
    monkeypatch.setattr(local_snapshot.LocalComputeSnapshotSource, "snapshot",
                        lambda self, node, *, now: NodeSnapshot(node=node, observed_at=now))
    owner = local_snapshot if component == "LocalComputeSnapshotSource" else bootstrap
    original = getattr(owner, component)
    entered, duplicate, release = Event(), Event(), Event()
    mutex, start = Lock(), Barrier(9)
    constructions = []
    def construct(*args, **kwargs):
        with mutex:
            constructions.append(True)
            entered.set()
            if len(constructions) > 1:
                duplicate.set()
        assert release.wait(5), "constructor release deadline"
        return original(*args, **kwargs)
    monkeypatch.setattr(owner, component, construct)
    app = bootstrap.build_application(config=SonderConfig())
    def use():
        start.wait(timeout=5)
        return getattr(app, accessor)()
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(use) for _ in range(8)]
            start.wait(timeout=5)
            assert entered.wait(5)
            duplicate.wait(.25)
            release.set()
            values = [future.result(timeout=10) for future in futures]
        assert len(constructions) == 1
        if accessor != "compute_snapshot":
            assert all(value is values[0] for value in values)
        assert app.compute_service()._registry is app.compute_registry()
    finally:
        release.set()
        app.close_providers()
