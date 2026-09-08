"""Regression coverage for the packaged application lifecycle adapter."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from types import ModuleType
import sys
import threading

import pytest

from sonder_runtime.adapters.application_lifecycle import ApplicationLifecycle
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.platform.config import SonderConfig, StateConfig


def test_lifecycle_lazily_caches_and_resets() -> None:
    calls: list[int] = []
    lifecycle = ApplicationLifecycle(lambda: calls.append(1) or object())

    first = lifecycle.get()
    assert lifecycle.get() is first
    assert calls == [1]

    lifecycle.reset()
    assert lifecycle.get() is not first
    assert calls == [1, 1]


def test_lifecycle_builds_once_under_concurrent_access() -> None:
    calls: list[int] = []
    lifecycle = ApplicationLifecycle(lambda: calls.append(1) or object())
    results: list[object] = []

    threads = [threading.Thread(target=lambda: results.append(lifecycle.get())) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1
    assert len({id(result) for result in results}) == 1


def test_bootstrap_compatibility_functions_use_packaged_lifecycle(monkeypatch) -> None:
    bootstrap_app.reset_for_tests()
    first = bootstrap_app.build_application()
    second = bootstrap_app.build_application()
    sentinels = iter((first, second))
    monkeypatch.setattr(bootstrap_app, "build_application", lambda: next(sentinels))

    assert bootstrap_app.default_app() is first
    assert bootstrap_app.default_app() is first

    bootstrap_app.reset_for_tests()
    assert bootstrap_app.default_app() is second
    bootstrap_app.reset_for_tests()


def test_default_runtime_cleanup_uses_one_bound_full_graph_close(monkeypatch, tmp_path) -> None:
    """The compatibility cleanup must not strand specialized providers."""
    bootstrap_app.reset_for_tests()
    application = bootstrap_app.build_application(
        config=SonderConfig(state=StateConfig(home=str(tmp_path / "state")))
    )
    calls = []

    def close(self, *, timeout=None):
        calls.append((self, timeout))

    monkeypatch.setattr(bootstrap_app.Application, "close_providers", close)
    monkeypatch.setattr(bootstrap_app._application_lifecycle, "get", lambda: application)
    try:
        assert bootstrap_app.default_app(config=application.config) is application
        bootstrap_app.close_default_runtime_resources(timeout=2)
        bootstrap_app.close_default_runtime_resources(timeout=2)
        assert calls == [(application, 2)]
    finally:
        bootstrap_app.reset_for_tests()


def test_default_compute_refuses_to_race_a_full_graph_owner(monkeypatch) -> None:
    bootstrap_app.reset_for_tests()
    monkeypatch.setattr(
        bootstrap_app,
        "_default_application_close",
        lambda *, timeout: None,
    )
    with pytest.raises(RuntimeError, match="full default graph owns compute cleanup"):
        bootstrap_app.close_default_compute()


def test_default_config_replacement_closes_the_prior_full_graph_once(monkeypatch, tmp_path) -> None:
    bootstrap_app.reset_for_tests()
    first = bootstrap_app.build_application(
        config=SonderConfig(state=StateConfig(home=str(tmp_path / "first")))
    )
    second = bootstrap_app.build_application(
        config=SonderConfig(state=StateConfig(home=str(tmp_path / "second")))
    )
    calls = []

    def close(self, *, timeout=None):
        calls.append(self)

    monkeypatch.setattr(bootstrap_app.Application, "close_providers", close)
    monkeypatch.setattr(
        bootstrap_app._application_lifecycle,
        "get",
        lambda: first if bootstrap_app._default_config is first.config else second,
    )
    try:
        assert bootstrap_app.default_app(config=first.config) is first
        assert bootstrap_app.default_app(config=second.config) is second
        assert calls == [first]
    finally:
        bootstrap_app.reset_for_tests()


def test_full_graph_close_attempts_every_resource_after_an_earlier_failure(tmp_path) -> None:
    application = bootstrap_app.build_application(
        config=SonderConfig(state=StateConfig(home=str(tmp_path / "state")))
    )
    calls = []

    def artifact_close():
        calls.append("artifact")
        raise RuntimeError("artifact close failed")

    def delegation_close(*, timeout=None):
        calls.append("delegation")

    def compute_close(*, timeout=None):
        calls.append("compute")

    def membership_close(*, timeout=None):
        calls.append("membership")
        return True

    def pool_drain(*, timeout_seconds=None):
        calls.append("pool")
        return True

    graph = replace(
        application,
        close_artifact_mobility=artifact_close,
        close_delegation=delegation_close,
        close_compute=compute_close,
        memory_replication=SimpleNamespace(close=lambda: calls.append("memory")),
        inference_pool=SimpleNamespace(drain=pool_drain),
        inference_membership=SimpleNamespace(close=membership_close),
        specialized_providers=SimpleNamespace(
            close=lambda *, timeout=None: calls.append("specialized")
        ),
    )

    with pytest.raises(RuntimeError, match="artifact close failed"):
        graph.close_providers(timeout=2)
    assert calls == [
        "artifact", "delegation", "compute", "memory", "pool", "membership", "specialized"
    ]


def test_full_graph_close_fences_inference_admission(tmp_path) -> None:
    from sonder_runtime.adapters.inference import ollama_pool
    from sonder_runtime.adapters.inference.ollama_pool import WorkerPoolDraining

    application = bootstrap_app.build_application(
        config=SonderConfig(state=StateConfig(home=str(tmp_path / "state")))
    )
    try:
        application.close_providers(timeout=2)
        with pytest.raises(WorkerPoolDraining, match="draining"):
            application.inference_pool.request(lambda _worker: object())
    finally:
        bootstrap_app.reset_for_tests()
        ollama_pool.reset_typed_workers()


def test_default_runtime_close_detaches_legacy_owner_before_full_cleanup(
    monkeypatch, tmp_path,
) -> None:
    from sonder_runtime.bootstrap import legacy_root

    bootstrap_app.reset_for_tests()
    application = bootstrap_app.build_application(
        config=SonderConfig(state=StateConfig(home=str(tmp_path / "state")))
    )
    calls = []
    legacy = ModuleType("server")
    legacy._APP_GRAPH = application
    legacy._APP_GRAPH_LOCK = threading.Lock()

    def close(self, *, timeout=None):
        calls.append((self, timeout))

    monkeypatch.setitem(sys.modules, "server", legacy)
    monkeypatch.setattr(legacy_root, "_owned_application", application)
    monkeypatch.setattr(bootstrap_app.Application, "close_providers", close)
    monkeypatch.setattr(bootstrap_app._application_lifecycle, "get", lambda: application)
    try:
        assert bootstrap_app.default_app(config=application.config) is application
        bootstrap_app.close_default_runtime_resources(timeout=2)
        assert legacy._APP_GRAPH is None
        assert legacy_root._owned_application is None
        assert calls == [(application, 2)]
    finally:
        bootstrap_app.reset_for_tests()


def test_default_runtime_close_still_closes_graph_when_legacy_detach_fails(
    monkeypatch, tmp_path,
) -> None:
    """A busy compatibility handoff cannot strand the graph's providers."""
    from sonder_runtime.bootstrap import legacy_root

    bootstrap_app.reset_for_tests()
    application = bootstrap_app.build_application(
        config=SonderConfig(state=StateConfig(home=str(tmp_path / "state")))
    )
    calls = []

    monkeypatch.setattr(
        legacy_root,
        "detach_owned_application",
        lambda _application: (_ for _ in ()).throw(RuntimeError("legacy busy")),
    )
    monkeypatch.setattr(
        bootstrap_app.Application,
        "close_providers",
        lambda self, *, timeout=None: calls.append((self, timeout)),
    )
    monkeypatch.setattr(bootstrap_app._application_lifecycle, "get", lambda: application)
    try:
        assert bootstrap_app.default_app(config=application.config) is application
        with pytest.raises(RuntimeError, match="legacy busy"):
            bootstrap_app.close_default_runtime_resources(timeout=2)
        assert calls == [(application, 2)]
    finally:
        bootstrap_app.reset_for_tests()


def test_concurrent_default_cleanup_claims_the_full_graph_once(monkeypatch) -> None:
    bootstrap_app.reset_for_tests()
    calls = []
    gate = threading.Barrier(3)
    monkeypatch.setattr(
        bootstrap_app,
        "_default_application_close",
        lambda *, timeout: calls.append(timeout),
    )

    def close():
        gate.wait()
        bootstrap_app.close_default_runtime_resources(timeout=2)

    first = threading.Thread(target=close)
    second = threading.Thread(target=close)
    first.start()
    second.start()
    gate.wait()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert calls == [2]


def test_default_app_refuses_replacement_while_prior_graph_cleanup_runs(
    monkeypatch, tmp_path,
) -> None:
    bootstrap_app.reset_for_tests()
    first = bootstrap_app.build_application(
        config=SonderConfig(state=StateConfig(home=str(tmp_path / "first")))
    )
    second = bootstrap_app.build_application(
        config=SonderConfig(state=StateConfig(home=str(tmp_path / "second")))
    )
    started = threading.Event()
    release = threading.Event()
    calls = []

    def close(self, *, timeout=None):
        calls.append(self)
        if self is first:
            started.set()
            assert release.wait(5)

    monkeypatch.setattr(bootstrap_app.Application, "close_providers", close)
    monkeypatch.setattr(
        bootstrap_app._application_lifecycle,
        "get",
        lambda: first if bootstrap_app._default_config is first.config else second,
    )
    try:
        assert bootstrap_app.default_app(config=first.config) is first
        closer = threading.Thread(
            target=lambda: bootstrap_app.close_default_runtime_resources(timeout=2),
        )
        closer.start()
        assert started.wait(5)
        with pytest.raises(RuntimeError, match="cleanup is in progress"):
            bootstrap_app.default_app(config=second.config)
        release.set()
        closer.join(timeout=5)
        assert not closer.is_alive()
        assert bootstrap_app.default_app(config=second.config) is second
        assert calls == [first]
    finally:
        release.set()
        bootstrap_app.reset_for_tests()


def test_direct_legacy_mcp_retires_only_its_locally_built_graph(monkeypatch):
    """The compatibility MCP finalizer cannot strand a server-created graph."""
    import server

    calls = []
    original_close = bootstrap_app.Application.close_providers
    monkeypatch.setattr(server, "_APP_GRAPH", None)
    monkeypatch.setattr(server, "_APP_GRAPH_OWNED_BY_SERVER", False)
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("adapter"))
    monkeypatch.setattr(server.OLLAMA_POOL, "drain", lambda **_kwargs: calls.append("pool"))

    def close(graph, timeout=None):
        calls.append(graph)
        return original_close(graph, timeout=timeout)

    monkeypatch.setattr(bootstrap_app.Application, "close_providers", close)
    graph = server._application()
    server.run_mcp(safety_checked=True)

    assert calls[0:2] == ["adapter", "pool"]
    assert calls[2] is graph
    assert server._APP_GRAPH is None
    assert server._APP_GRAPH_OWNED_BY_SERVER is False


def test_direct_legacy_mcp_retires_a_locally_built_graph_when_binding_is_refused(
    monkeypatch,
):
    """A pre-run binding rejection cannot defer local graph cleanup to atexit."""
    import server
    from sonder_runtime.bootstrap import legacy_root
    from sonder_runtime.adapters.inference.ollama_pool import WorkerPoolUnavailable

    calls = []
    original_close = bootstrap_app.Application.close_providers
    monkeypatch.setattr(server, "_APP_GRAPH", None)
    monkeypatch.setattr(server, "_APP_GRAPH_OWNED_BY_SERVER", False)
    monkeypatch.setattr(
        legacy_root,
        "require_mcp_inference_binding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("refused")),
    )
    monkeypatch.setattr(server.OLLAMA_POOL, "drain", lambda **_kwargs: calls.append("pool"))

    def close(graph, timeout=None):
        calls.append(graph)
        return original_close(graph, timeout=timeout)

    monkeypatch.setattr(bootstrap_app.Application, "close_providers", close)
    graph = server._application()

    with pytest.raises(WorkerPoolUnavailable, match="trusted application membership"):
        server.run_mcp(safety_checked=True)

    assert calls == [graph]
    assert server._APP_GRAPH is None
    assert server._APP_GRAPH_OWNED_BY_SERVER is False


def test_direct_legacy_mcp_retires_a_locally_built_graph_when_safety_refuses(
    monkeypatch,
):
    """A gate refusal cannot leave a direct server graph alive until atexit."""
    import server

    calls = []
    original_close = bootstrap_app.Application.close_providers
    monkeypatch.setattr(server, "_APP_GRAPH", None)
    monkeypatch.setattr(server, "_APP_GRAPH_OWNED_BY_SERVER", False)
    monkeypatch.setattr(
        server,
        "require_mcp_startup_safety",
        lambda: (_ for _ in ()).throw(RuntimeError("safety refused")),
    )

    def close(graph, timeout=None):
        calls.append(graph)
        return original_close(graph, timeout=timeout)

    monkeypatch.setattr(bootstrap_app.Application, "close_providers", close)
    graph = server._application()

    with pytest.raises(RuntimeError, match="safety refused"):
        server.run_mcp()

    assert calls == [graph]
    assert server._APP_GRAPH is None
    assert server._APP_GRAPH_OWNED_BY_SERVER is False
