"""BuildModelService: principal-keyed LRU cache, fingerprint invalidation,
TTL, refresh, and a brief summary that never reads."""
from __future__ import annotations

import sys
import types
import uuid

import pytest

from sonder_runtime.application.build.model_service import BuildModelService, LruBuildModelCache
from sonder_runtime.application.build.ports import BuildModelRequest, BuildTreeLocation
from sonder_runtime.application.context import OperationContext, local_owner_context
from sonder_runtime.domain.common.errors import InvalidInput

pytestmark = pytest.mark.unit


class Never:
    cancelled = False

    def wait(self, timeout=None):
        return False


def ctx(principal=None):
    base = local_owner_context(correlation_id=uuid.uuid4().hex)
    if principal is None:
        return base
    return OperationContext(correlation_id="c", principal_id=principal, auth_level="user",
                            source="http", deadline_monotonic=None, cancellation=Never())


class Model:
    def __init__(self, label, n=1):
        self.project_label = label
        self.units = tuple(range(n))
        self.targets = ()


class SpyReader:
    def __init__(self):
        self.fp = "one"
        self.calls = 0

    def fingerprint(self, root, build_dir):
        self.calls += 1
        return self.fp


class SpyPlanner:
    def __init__(self):
        self.builds = 0
        self.locates = 0

    def locate(self, request, context):
        self.locates += 1
        return BuildTreeLocation("/src/" + request.project, "/src/%s/build" % request.project,
                                 request.project, "build")

    def plan_model(self, request, context, *, location=None):
        self.builds += 1
        return Model(location.project_label)


@pytest.fixture
def domain_summary(monkeypatch):
    """``domain.build.model.build_context_summary`` (lane A), or a stand-in until it lands."""
    def summary(model, *, max_chars=600):
        return ("build %s" % model.project_label)[:max_chars]

    try:
        import sonder_runtime.domain.build.model as real
    except ImportError:
        real = None
    if real is not None:  # the fakes here are not domain models
        monkeypatch.setattr(real, "build_context_summary", summary)
        return
    package = types.ModuleType("sonder_runtime.domain.build")
    package.__path__ = []
    module = types.ModuleType("sonder_runtime.domain.build.model")
    module.build_context_summary = summary
    monkeypatch.setitem(sys.modules, "sonder_runtime.domain.build", package)
    monkeypatch.setitem(sys.modules, "sonder_runtime.domain.build.model", module)


def make(ttl=600):
    now = {"t": 0.0}
    reader, planner = SpyReader(), SpyPlanner()
    service = BuildModelService(reader, planner, LruBuildModelCache(), clock=lambda: now["t"],
                                ttl_seconds=ttl)
    return service, reader, planner, now


def test_models_are_cached_per_principal():
    service, _, planner, _ = make()
    first = service.model(BuildModelRequest(project="a"), ctx())
    assert service.model(BuildModelRequest(project="a"), ctx()) is first
    assert planner.builds == 1
    service.model(BuildModelRequest(project="a"), ctx("bob"))
    assert planner.builds == 2  # never shared across principals


def test_a_new_fingerprint_or_refresh_or_ttl_rebuilds():
    service, reader, planner, now = make(ttl=10)
    service.model(BuildModelRequest(project="a"), ctx())
    reader.fp = "two"
    service.model(BuildModelRequest(project="a"), ctx())
    assert planner.builds == 2
    service.model(BuildModelRequest(project="a", refresh=True), ctx())
    assert planner.builds == 3
    now["t"] = 11
    service.model(BuildModelRequest(project="a"), ctx())
    assert planner.builds == 4


def test_cached_summary_never_reads_and_is_principal_scoped(domain_summary):
    service, reader, planner, _ = make()
    assert service.cached_summary("local-owner", "a") == ""
    service.model(BuildModelRequest(project="a"), ctx())
    reads = (reader.calls, planner.locates, planner.builds)
    summary = service.cached_summary(ctx().principal_id, "a")
    assert "a" in summary and len(summary) <= 600
    assert service.cached_summary("bob", "a") == ""
    assert service.cached_summary(ctx().principal_id, "other") == ""
    assert (reader.calls, planner.locates, planner.builds) == reads


def test_the_lru_is_bounded_by_count_and_bytes():
    cache = LruBuildModelCache(max_entries=2, max_bytes=10_000_000,
                               estimate=lambda model: len(model.units))
    for index in range(3):
        cache.put(("p", index), Model("x"), stored_at=0)
    assert len(cache) == 2 and cache.get(("p", 0)) is None
    small = LruBuildModelCache(max_entries=8, max_bytes=100, estimate=lambda model: len(model.units))
    small.put(("p", 1), Model("x", 60), stored_at=0)
    small.put(("p", 2), Model("x", 60), stored_at=0)
    assert len(small) == 1 and small.estimated_bytes == 60
    small.put(("p", 3), Model("x", 500), stored_at=0)  # larger than the budget: never cached
    assert small.get(("p", 3)) is None


def test_invalidate_drops_the_tree():
    service, _, planner, _ = make()
    service.model(BuildModelRequest(project="a"), ctx())
    service.invalidate(ctx().principal_id, "/src/a", "/src/a/build")
    assert service.cached_model(ctx().principal_id, "/src/a", "/src/a/build") is None
    service.model(BuildModelRequest(project="a"), ctx())
    assert planner.builds == 2


def test_view_rejects_bad_detail_and_bounds():
    service, _, _, _ = make()
    with pytest.raises(InvalidInput):
        service.view(BuildModelRequest(), ctx(), detail="secrets")
    with pytest.raises(InvalidInput):
        service.view(BuildModelRequest(), ctx(), max_items=0)
