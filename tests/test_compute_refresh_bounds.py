from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, Lock, Thread

from tests.test_compute_snapshot_refresh import _node, NOW
from sonder_runtime.application.compute_fabric import refresh
from sonder_runtime.application.compute_fabric.registry import ComputeNodeRegistry
from sonder_runtime.domain.compute_fabric import NodeSnapshot, NodeHealth


def test_refresh_bounds_submitted_work_not_only_running_threads(monkeypatch):
    release = Event()
    overflow = Event()
    first = Event()
    lock = Lock()
    submitted = 0
    class Executor(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            nonlocal submitted
            with lock:
                submitted += 1
                if submitted > 2 and not release.is_set():
                    overflow.set()
            return super().submit(*args, **kwargs)
    class Source:
        def snapshot(self, node, *, now):
            first.set()
            assert release.wait(5)
            return NodeSnapshot(node=node, observed_at=now, health=NodeHealth.HEALTHY)
    monkeypatch.setattr(refresh, 'ThreadPoolExecutor', Executor)
    registry = ComputeNodeRegistry(tuple(_node(f'node-{i}') for i in range(32)))
    failures = []
    def run():
        try:
            refresh.refresh_remote_snapshots(registry, Source(), now=lambda: NOW, max_workers=2)
        except Exception as error:
            failures.append(error)
    thread = Thread(target=run)
    thread.start()
    try:
        assert first.wait(2)
        overqueued = overflow.wait(.2)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not failures
    assert not overqueued, 'refresh eagerly queued the entire inventory behind blocked probes'
    assert len(registry.list_snapshots(now=NOW)) == 32


def test_recent_healthy_observation_avoids_another_network_probe():
    node = _node('worker')
    registry = ComputeNodeRegistry((node,))
    registry.observe(NodeSnapshot(node=node, observed_at=NOW, health=NodeHealth.HEALTHY))
    class Source:
        calls = 0
        def snapshot(self, node, *, now):
            self.calls += 1
            return NodeSnapshot(node=node, observed_at=now, health=NodeHealth.HEALTHY)
    source = Source()
    refresh.refresh_remote_snapshots(registry, source, now=lambda: NOW + timedelta(seconds=2),
                                     refresh_after=timedelta(seconds=10))
    assert source.calls == 0
    refresh.refresh_remote_snapshots(registry, source, now=lambda: NOW + timedelta(seconds=11),
                                     refresh_after=timedelta(seconds=10))
    assert source.calls == 1


def test_unhealthy_observation_does_not_hide_behind_fresh_receipt():
    node = _node('worker')
    registry = ComputeNodeRegistry((node,))
    registry.observe(NodeSnapshot(node=node, observed_at=NOW, health=NodeHealth.UNHEALTHY))
    class Source:
        def snapshot(self, node, *, now):
            return NodeSnapshot(node=node, observed_at=now, health=NodeHealth.HEALTHY)
    refresh.refresh_remote_snapshots(registry, Source(), now=lambda: NOW,
                                     refresh_after=timedelta(seconds=10))
    assert registry.last_observation(node.node_id).health is NodeHealth.HEALTHY
