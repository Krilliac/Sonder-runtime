"""HostToolInventoryService: single flight, TTL, failure fallback, views."""
import threading
import time

import pytest

from sonder_runtime.application.host_tools.service import HostToolInventoryService
from sonder_runtime.domain.common.errors import DependencyUnavailable, InvalidInput
from sonder_runtime.domain.host_tools.model import (
    DiscoverySource,
    ToolCategory,
    ToolRecord,
    VersionStatus,
    build_snapshot,
)


def _snapshot(created_at, names=("gcc",)):
    records = [
        ToolRecord(name=name, category=ToolCategory.COMPILER, path=f"/home/alice/bin/{name}",
                   source=DiscoverySource.PATH, on_path=True, version="13.2.0",
                   version_status=VersionStatus.OK, identity="1:1")
        for name in names
    ]
    return build_snapshot(os="Linux", os_release="x", machine="x86_64", created_at=created_at,
                          duration_ms=1, tools=records)


class Discovery:
    def __init__(self, clock, *, delay=0.0, fail=False):
        self.calls = []
        self.clock = clock
        self.delay = delay
        self.fail = fail

    def discover(self, *, previous, full):
        self.calls.append((previous, full))
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise OSError("boom")
        return _snapshot(self.clock(), names=("gcc", "clang"))


class Store:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot
        self.saved = []
        self.loads = 0

    def load(self):
        self.loads += 1
        return self.snapshot

    def save(self, snapshot):
        self.saved.append(snapshot)


def _service(discovery, store, now, **kwargs):
    return HostToolInventoryService(
        discovery, store, clock=lambda: now[0], ttl_seconds=100,
        redact_path=lambda p: p.replace("/home/alice", "~"),
        executable_guard=kwargs.pop("guard", lambda p: True), **kwargs,
    )


def test_concurrent_refreshes_share_one_discovery():
    now = [1000.0]
    discovery = Discovery(lambda: now[0], delay=0.3)
    service = _service(discovery, Store(), now)
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()
        results.append(service.snapshot(refresh=True))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(discovery.calls) == 1
    assert results[0] is results[1]


def test_stale_snapshot_triggers_discovery_and_fresh_does_not():
    now = [1000.0]
    discovery = Discovery(lambda: now[0])
    store = Store(_snapshot(950.0))
    service = _service(discovery, store, now)
    assert service.snapshot().created_at == 950.0 and discovery.calls == []
    now[0] = 1050.0
    assert service.snapshot().created_at == 1050.0
    assert len(discovery.calls) == 1 and store.saved
    assert discovery.calls[0][0].created_at == 950.0  # previous passed for the version cache
    service.snapshot(full=True)
    assert discovery.calls[-1] == (None, True)


def test_failed_refresh_keeps_previous_with_a_note():
    now = [1000.0]
    discovery = Discovery(lambda: now[0], fail=True)
    service = _service(discovery, Store(_snapshot(990.0)), now)
    snapshot = service.snapshot(refresh=True)
    assert [t.name for t in snapshot.tools] == ["gcc"]
    assert "refresh failed: OSError" in snapshot.notes
    empty = _service(Discovery(lambda: now[0], fail=True), Store(), now)
    with pytest.raises(DependencyUnavailable):
        empty.snapshot()


def test_view_filters_redacts_and_validates_before_discovery():
    now = [1000.0]
    discovery = Discovery(lambda: now[0])
    service = _service(discovery, Store(_snapshot(990.0, names=("gcc", "clang"))), now)
    view = service.view(name="gcc")
    assert [t.name for t in view.tools] == ["gcc"]
    assert view.tools[0].path_display == "~/bin/gcc"
    assert service.view(redacted=False).tools[0].path_display.startswith("/home/alice")
    with pytest.raises(InvalidInput):
        service.view(category="spaceship")
    with pytest.raises(InvalidInput):
        service.view(name="../gcc")
    assert discovery.calls == []


def test_capability_summary_never_discovers_and_never_raises():
    now = [1000.0]
    discovery = Discovery(lambda: now[0])
    service = _service(discovery, Store(), now)
    assert service.capability_summary() == ""
    assert discovery.calls == []
    stale = _service(discovery, Store(_snapshot(1.0)), now)
    assert stale.capability_summary().startswith("compilers: gcc 13.2")
    assert discovery.calls == []

    class Exploding(Store):
        def load(self):
            raise RuntimeError("disk on fire")

    assert _service(discovery, Exploding(), now).capability_summary() == ""


def test_lookup_applies_the_executable_guard():
    now = [1000.0]
    allowed = _service(Discovery(lambda: now[0]), Store(_snapshot(990.0)), now)
    assert allowed.lookup("GCC").name == "gcc"
    denied = _service(Discovery(lambda: now[0]), Store(_snapshot(990.0)), now, guard=lambda p: False)
    assert denied.lookup("gcc") is None
    raising = _service(Discovery(lambda: now[0]), Store(_snapshot(990.0)), now,
                       guard=lambda p: (_ for _ in ()).throw(OSError()))
    assert raising.lookup("gcc") is None


def test_cached_and_summary_do_not_wait_behind_a_running_discovery():
    now = [1000.0]
    started = threading.Event()
    release = threading.Event()

    class Slow:
        def discover(self, *, previous, full):
            started.set()
            release.wait(10)
            return _snapshot(now[0], names=("gcc", "clang"))

    service = _service(Slow(), Store(_snapshot(990.0)), now)
    worker = threading.Thread(target=lambda: service.snapshot(refresh=True))
    worker.start()
    try:
        assert started.wait(5)
        began = time.monotonic()
        cached = service.cached()
        summary = service.capability_summary()
        elapsed = time.monotonic() - began
        assert cached is not None and cached.created_at == 990.0
        assert summary.startswith("compilers: gcc")
        assert elapsed < 1.0, "the agent brief blocked behind a discovery"
    finally:
        release.set()
        worker.join(10)
    assert service.cached().created_at == 1000.0
