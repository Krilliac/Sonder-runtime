"""Unit tests for non-blocking cached model advertisement probes in legacy_chat_bridge."""
from __future__ import annotations

import gc
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters import legacy_chat_bridge
from sonder_runtime.adapters.provider_bindings import ProviderBindings
from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
from sonder_runtime.platform.runtime_threads import ThreadOwnershipRefused


class _FakeClock:
    def __init__(self, initial: float = 100.0) -> None:
        self._lock = threading.Lock()
        self._now = float(initial)

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> float:
        with self._lock:
            self._now += float(seconds)
            return self._now


class _StubGateway:
    def __init__(
        self,
        *,
        models: list[str] | None = None,
        health_models: list[object] | None = None,
        health_state: str = "ready",
        base_url: str = "http://127.0.0.1:11437",
        workers: tuple[object, ...] = (),
        error: Exception | None = None,
        health_error: Exception | None = None,
        block_event: threading.Event | None = None,
        entered_event: threading.Event | None = None,
        healthy_worker: bool = True,
        include_ollama_pool_models: list[str] | None = None,
    ) -> None:
        self.models = list(models) if models is not None else []
        self.health_models = list(health_models) if health_models is not None else []
        self.health_state = health_state
        self.base_url = base_url
        self.workers = workers
        self.error = error
        self.health_error = health_error
        self.block_event = block_event
        self.entered_event = entered_event
        self.healthy_worker = healthy_worker
        self.include_ollama_pool_models = list(include_ollama_pool_models or [])
        self._lock = threading.Lock()
        self.status_calls = 0
        self.health_calls = 0

    def settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            base_url=self.base_url,
            display_base_url=self.base_url,
            workers=self.workers,
        )

    def unified_worker_status(self, ollama_pool=None) -> dict[str, object]:
        with self._lock:
            self.status_calls += 1
        if self.entered_event is not None:
            self.entered_event.set()
        if self.block_event is not None:
            self.block_event.wait(timeout=5.0)
        if self.error is not None:
            raise self.error
        rows: list[dict[str, object]] = []
        if self.include_ollama_pool_models:
            rows.append({
                "source": "ollama_pool",
                "origin": "http://127.0.0.1:11434",
                "healthy": True,
                "models": list(self.include_ollama_pool_models),
            })
        rows.append({
            "source": "sonder_inference_worker",
            "origin": "https://10.77.0.2:8443",
            "healthy": self.healthy_worker,
            "models": list(self.models),
        })
        return {"workers": rows}

    def health(self) -> SimpleNamespace:
        with self._lock:
            self.health_calls += 1
        if self.health_error is not None:
            raise self.health_error
        return SimpleNamespace(
            state=self.health_state,
            document={"models": list(self.health_models)},
        )


def _graph_for(gw: object, *, provider: str = "sonder_inference") -> SimpleNamespace:
    bindings = ProviderBindings(
        default_generation_provider=provider,
        tier_providers={
            "fast": provider,
            "general": provider,
            "code": provider,
            "reasoning": provider,
            "vision": "ollama",
        },
        embedding_provider="ollama",
    )
    providers: dict[str, object] = {"ollama": SimpleNamespace()}
    providers[provider] = gw
    dispatch = ProviderDispatchGateway(
        providers=providers,  # type: ignore[arg-type]
        tier_providers=dict(bindings.tier_providers),
        default_generation_provider=provider,
        embedding_provider="ollama",
    )
    return SimpleNamespace(provider_bindings=bindings, model_gateway=dispatch)


@pytest.fixture(autouse=True)
def _reset_bridge_cache():
    legacy_chat_bridge.reset_model_advertisement_cache()
    yield
    legacy_chat_bridge.reset_model_advertisement_cache()


def test_warm_cache_never_blocks_or_reprobes():
    clock = _FakeClock()
    cache = legacy_chat_bridge._ModelAdvertisementCache(
        monotonic=clock,
        positive_ttl_seconds=30.0,
        negative_ttl_seconds=5.0,
        stale_ttl_seconds=120.0,
        wait_timeout_seconds=0.25,
    )
    gw = _StubGateway(models=["qwen3.6:35b", "astra-cyber-35b-a3b:latest"])

    assert cache.advertises_model(gw, "qwen3.6:35b") is True
    assert gw.status_calls == 1

    # Even if the worker subsequently hangs, warm cache hits return immediately
    # with zero additional probes.
    never_set = threading.Event()
    gw.block_event = never_set
    clock.advance(15.0)

    started = time.monotonic()
    for _ in range(50):
        assert cache.advertises_model(gw, "qwen3.6:35b") is True
        assert cache.advertises_model(gw, "qwen3.6") is True
        assert cache.advertises_model(gw, "QWEN3.6:35B") is True
    elapsed = time.monotonic() - started

    assert gw.status_calls == 1
    assert elapsed < 0.2


def test_concurrent_thundering_herd_coalesces_to_single_probe():
    clock = _FakeClock()
    cache = legacy_chat_bridge._ModelAdvertisementCache(
        monotonic=clock,
        positive_ttl_seconds=30.0,
        negative_ttl_seconds=5.0,
        stale_ttl_seconds=120.0,
        wait_timeout_seconds=1.0,
    )
    entered = threading.Event()
    release = threading.Event()
    gw = _StubGateway(
        models=["qwen3.6:35b", "coder-80b:q4"],
        block_event=release,
        entered_event=entered,
    )

    caller_count = 20
    barrier = threading.Barrier(caller_count)
    results: list[tuple[str, bool]] = []
    results_lock = threading.Lock()
    models_to_ask = [
        "qwen3.6:35b" if idx % 3 == 0 else ("coder-80b" if idx % 3 == 1 else "ollama-only")
        for idx in range(caller_count)
    ]

    def worker(model_name: str) -> None:
        barrier.wait(timeout=5.0)
        answer = cache.advertises_model(gw, model_name)
        with results_lock:
            results.append((model_name, answer))

    threads = [threading.Thread(target=worker, args=(m,), daemon=True) for m in models_to_ask]
    for t in threads:
        t.start()

    assert entered.wait(timeout=2.0)
    # Give peer threads a moment to arrive and coalesce on the in-flight probe.
    time.sleep(0.03)
    release.set()

    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive()

    assert gw.status_calls == 1
    assert len(results) == caller_count
    for model_name, answer in results:
        expected = model_name != "ollama-only"
        assert answer is expected


def test_stale_positive_triggers_non_blocking_background_refresh_and_hard_expiry_refreshes():
    clock = _FakeClock()
    cache = legacy_chat_bridge._ModelAdvertisementCache(
        monotonic=clock,
        positive_ttl_seconds=30.0,
        negative_ttl_seconds=5.0,
        stale_ttl_seconds=120.0,
        wait_timeout_seconds=0.5,
    )
    gw = _StubGateway(models=["qwen3.6:35b"])
    assert cache.advertises_model(gw, "qwen3.6:35b") is True
    assert gw.status_calls == 1

    # Enter stale-while-revalidate window [30s, 120s) with a slow refresh probe.
    refresh_entered = threading.Event()
    refresh_release = threading.Event()
    gw.models = ["replacement-model:latest"]
    gw.entered_event = refresh_entered
    gw.block_event = refresh_release
    clock.advance(35.0)

    started = time.monotonic()
    # Must return stale positive IMMEDIATELY without blocking on the slow worker.
    assert cache.advertises_model(gw, "qwen3.6:35b") is True
    # Subsequent hot-path calls while refresh is in flight also return immediately
    # and do not spawn duplicate refresh probes.
    assert cache.advertises_model(gw, "qwen3.6:35b") is True
    elapsed = time.monotonic() - started
    assert elapsed < 0.15
    assert refresh_entered.wait(timeout=2.0)
    assert gw.status_calls == 2

    # Once the background refresh completes, subsequent calls observe the fresh set.
    refresh_release.set()
    deadline = time.monotonic() + 2.0
    while cache.inflight_count > 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert cache.inflight_count == 0
    assert cache.advertises_model(gw, "qwen3.6:35b") is False
    assert cache.advertises_model(gw, "replacement-model:latest") is True
    assert gw.status_calls == 2

    # Advance past hard stale TTL (120s): next call refreshes synchronously within wait budget.
    gw.block_event = None
    gw.entered_event = None
    gw.models = ["hard-expired-model:7b"]
    clock.advance(125.0)
    assert cache.advertises_model(gw, "replacement-model:latest") is False
    assert cache.advertises_model(gw, "hard-expired-model:7b") is True
    assert gw.status_calls == 3


def test_short_negative_cache_for_absent_models_and_failing_workers():
    clock = _FakeClock()
    cache = legacy_chat_bridge._ModelAdvertisementCache(
        monotonic=clock,
        positive_ttl_seconds=30.0,
        negative_ttl_seconds=5.0,
        stale_ttl_seconds=120.0,
        wait_timeout_seconds=0.5,
    )

    # 1. Failing worker (raises exception) -> negative cached for 5s, recovers after 5s.
    failing_gw = _StubGateway(
        error=ConnectionRefusedError("down"),
        health_error=ConnectionRefusedError("down"),
    )
    assert cache.advertises_model(failing_gw, "qwen3.6:35b") is False
    assert failing_gw.status_calls == 1

    clock.advance(2.0)
    assert cache.advertises_model(failing_gw, "qwen3.6:35b") is False
    assert failing_gw.status_calls == 1  # negative cache suppresses retry storm

    # Worker recovers; once negative TTL expires, probe succeeds.
    failing_gw.error = None
    failing_gw.health_error = None
    failing_gw.models = ["qwen3.6:35b"]
    clock.advance(3.5)  # total 5.5s >= negative_ttl 5.0s
    assert cache.advertises_model(failing_gw, "qwen3.6:35b") is True
    assert failing_gw.status_calls == 2

    # 2. Absent model on a healthy worker -> negative cached for 5s, discovers
    # newly loaded model once negative TTL expires.
    assert cache.advertises_model(failing_gw, "brand-new:14b") is False
    assert failing_gw.status_calls == 2

    failing_gw.models.append("brand-new:14b")
    clock.advance(2.0)
    assert cache.advertises_model(failing_gw, "brand-new:14b") is False
    assert failing_gw.status_calls == 2

    clock.advance(3.5)
    assert cache.advertises_model(failing_gw, "brand-new:14b") is True
    assert failing_gw.status_calls == 3


def test_unhealthy_worker_and_unavailable_health_uses_negative_ttl():
    clock = _FakeClock()
    cache = legacy_chat_bridge._ModelAdvertisementCache(
        monotonic=clock,
        positive_ttl_seconds=30.0,
        negative_ttl_seconds=5.0,
        stale_ttl_seconds=120.0,
        wait_timeout_seconds=0.5,
    )
    gw = _StubGateway(
        models=[],
        healthy_worker=False,
        health_models=[],
        health_state="unavailable",
    )
    assert cache.advertises_model(gw, "qwen3.6:35b") is False
    assert gw.status_calls == 1
    assert gw.health_calls == 1

    # Within negative TTL: zero re-probes.
    clock.advance(3.0)
    assert cache.advertises_model(gw, "qwen3.6:35b") is False
    assert gw.status_calls == 1

    # After negative TTL: worker recovers and advertises model.
    gw.healthy_worker = True
    gw.models = ["qwen3.6:35b"]
    clock.advance(3.0)
    assert cache.advertises_model(gw, "qwen3.6:35b") is True
    assert gw.status_calls == 2


def test_slow_hung_worker_does_not_stall_hot_path_or_spawn_duplicate_probes():
    clock = _FakeClock()
    cache = legacy_chat_bridge._ModelAdvertisementCache(
        monotonic=clock,
        positive_ttl_seconds=30.0,
        negative_ttl_seconds=5.0,
        stale_ttl_seconds=120.0,
        wait_timeout_seconds=0.05,
    )
    entered = threading.Event()
    release = threading.Event()
    gw = _StubGateway(
        models=["qwen3.6:35b"],
        block_event=release,
        entered_event=entered,
    )
    try:
        # Cold miss bounds wait strictly to ~wait_timeout_seconds (50ms), not 5s.
        t0 = time.monotonic()
        assert cache.advertises_model(gw, "qwen3.6:35b") is False
        cold_elapsed = time.monotonic() - t0
        assert entered.wait(timeout=1.0)
        assert cold_elapsed < 0.4
        assert gw.status_calls == 1

        # Subsequent turns within negative TTL return immediately (0ms).
        t1 = time.monotonic()
        for _ in range(20):
            assert cache.advertises_model(gw, "qwen3.6:35b") is False
        assert time.monotonic() - t1 < 0.25
        assert gw.status_calls == 1

        # Even AFTER negative TTL expires while the first probe is STILL hung,
        # calls return immediately and NEVER spawn a duplicate probe.
        clock.advance(10.0)
        t2 = time.monotonic()
        assert cache.advertises_model(gw, "qwen3.6:35b") is False
        assert time.monotonic() - t2 < 0.25
        assert gw.status_calls == 1

        # Once the slow probe finally completes, cache populates automatically.
        release.set()
        deadline = time.monotonic() + 2.0
        while cache.inflight_count > 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert cache.inflight_count == 0
        assert cache.advertises_model(gw, "qwen3.6:35b") is True
        assert gw.status_calls == 1
    finally:
        release.set()


def test_cache_size_cap_lru_eviction_and_untrusted_model_flood_protection():
    clock = _FakeClock()
    cache = legacy_chat_bridge._ModelAdvertisementCache(
        monotonic=clock,
        positive_ttl_seconds=30.0,
        negative_ttl_seconds=5.0,
        stale_ttl_seconds=120.0,
        wait_timeout_seconds=0.5,
        max_entries=4,
    )

    gateways = [_StubGateway(models=[f"m-{i}:latest"], base_url=f"http://127.0.0.1:{11430 + i}") for i in range(6)]
    for i in range(4):
        assert cache.advertises_model(gateways[i], f"m-{i}:latest") is True
    assert cache.size == 4

    # Touch gateway 0 so gateway 1 becomes the least recently used.
    assert cache.advertises_model(gateways[0], "m-0:latest") is True
    assert gateways[0].status_calls == 1

    # Adding gateway 4 evicts gateway 1 (LRU), keeping gateway 0 warm.
    assert cache.advertises_model(gateways[4], "m-4:latest") is True
    assert cache.size == 4
    assert cache.advertises_model(gateways[0], "m-0:latest") is True
    assert gateways[0].status_calls == 1
    assert cache.advertises_model(gateways[1], "m-1:latest") is True
    assert gateways[1].status_calls == 2  # evicted earlier, re-probed

    # 200 distinct unknown model names against one gateway consume 0 extra
    # cache entries and trigger 0 extra probes during negative TTL.
    before_calls = gateways[0].status_calls
    for n in range(200):
        assert cache.advertises_model(gateways[0], f"attacker-probe-{n}") is False
    assert gateways[0].status_calls == before_calls
    assert cache.size == 4


def test_global_inflight_cap_prevents_thread_exhaustion():
    clock = _FakeClock()
    cache = legacy_chat_bridge._ModelAdvertisementCache(
        monotonic=clock,
        positive_ttl_seconds=30.0,
        negative_ttl_seconds=5.0,
        stale_ttl_seconds=120.0,
        wait_timeout_seconds=0.03,
        max_inflight=2,
    )
    release = threading.Event()
    gateways = [
        _StubGateway(models=["m:1"], base_url=f"http://127.0.0.1:{12000 + i}", block_event=release)
        for i in range(5)
    ]
    try:
        for gw in gateways:
            assert cache.advertises_model(gw, "m:1") is False
        assert cache.inflight_count <= 2
        probed = sum(gw.status_calls for gw in gateways)
        assert probed <= 2
    finally:
        release.set()
        deadline = time.monotonic() + 2.0
        while cache.inflight_count > 0 and time.monotonic() < deadline:
            time.sleep(0.005)


def test_scope_key_isolates_endpoints_workers_and_redacts_credentials(caplog):
    clock = _FakeClock()
    cache = legacy_chat_bridge._ModelAdvertisementCache(
        monotonic=clock,
        positive_ttl_seconds=30.0,
        negative_ttl_seconds=5.0,
        stale_ttl_seconds=120.0,
        wait_timeout_seconds=0.5,
    )
    secret_url = "https://admin:super-secret-token@10.77.0.2:8443/v1?api_key=leakme#frag"
    gw = _StubGateway(models=["model-a:latest"], base_url=secret_url)

    scope = legacy_chat_bridge._gateway_probe_scope(gw)
    assert scope[2] == "https://10.77.0.2:8443"
    assert "super-secret-token" not in repr(scope)
    assert "leakme" not in repr(scope)

    assert cache.advertises_model(gw, "model-a:latest") is True
    assert gw.status_calls == 1

    # Changing configured endpoint invalidates scope cleanly without waiting for TTL.
    gw.base_url = "http://127.0.0.1:11438"
    gw.models = ["model-b:latest"]
    assert cache.advertises_model(gw, "model-a:latest") is False
    assert cache.advertises_model(gw, "model-b:latest") is True
    assert gw.status_calls == 2

    # Changing configured private workers also changes scope cleanly.
    gw.workers = (SimpleNamespace(display_url="https://10.77.0.3:8443", url="https://10.77.0.3:8443"),)
    gw.models = ["model-c:latest"]
    assert cache.advertises_model(gw, "model-c:latest") is True
    assert gw.status_calls == 3

    # Failing probe logs only exception class name, never secret error details.
    gw.base_url = "http://127.0.0.1:11439"
    gw.error = RuntimeError("Bearer top-secret-credential-xyz")
    gw.health_error = RuntimeError("Bearer top-secret-credential-xyz")
    with caplog.at_level(logging.DEBUG, logger="sonder.server"):
        assert cache.advertises_model(gw, "model-c:latest") is False
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "top-secret-credential-xyz" not in logged


def test_reset_detaches_late_inflight_probes():
    clock = _FakeClock()
    cache = legacy_chat_bridge._ModelAdvertisementCache(
        monotonic=clock,
        positive_ttl_seconds=30.0,
        negative_ttl_seconds=5.0,
        stale_ttl_seconds=120.0,
        wait_timeout_seconds=0.04,
    )
    entered = threading.Event()
    release = threading.Event()
    gw = _StubGateway(models=["stale-from-old-gen:latest"], block_event=release, entered_event=entered)
    try:
        assert cache.advertises_model(gw, "stale-from-old-gen:latest") is False
        assert entered.wait(timeout=1.0)
        cache.reset()
        assert cache.size == 0
        assert cache.inflight_count == 0

        # Release the pre-reset probe; its late result must NOT pollute the reset cache.
        release.set()
        time.sleep(0.05)
        assert cache.size == 0
    finally:
        release.set()


def test_routing_semantics_health_dict_models_ollama_pool_isolation_and_prefix_matching():
    # 1. ollama_pool models are ignored; health() fallback accepts dict {"id": ...} items.
    gw_health_dicts = _StubGateway(
        models=[],
        include_ollama_pool_models=["ollama-only:7b"],
        health_models=[{"id": "qwen3.6:35b"}, {"id": "mix:8x7b:q4"}],
    )
    graph = _graph_for(gw_health_dicts)
    assert legacy_chat_bridge.provider_for_tier("model:qwen3.6:35b", False, graph) == "sonder_inference"
    assert legacy_chat_bridge.provider_for_tier("model:qwen3.6", False, graph) == "sonder_inference"
    assert legacy_chat_bridge.provider_for_tier("model:qwen3.6-extra", False, graph) is None
    assert legacy_chat_bridge.provider_for_tier("model:mix:8x7b", False, graph) is None
    assert legacy_chat_bridge.provider_for_tier("model:mix", False, graph) == "sonder_inference"
    assert legacy_chat_bridge.provider_for_tier("model:ollama-only:7b", False, graph) is None
    # Cloud turns never probe or route to sonder_inference.
    assert legacy_chat_bridge.provider_for_tier("model:qwen3.6:35b", True, graph) is None
    assert gw_health_dicts.status_calls == 1
    assert gw_health_dicts.health_calls == 1

    # 2. health() fallback also preserves compatibility with plain string model lists.
    gw_health_strings = _StubGateway(models=[], health_models=["plain-str-model:latest"])
    graph_str = _graph_for(gw_health_strings)
    assert legacy_chat_bridge.provider_for_tier("model:plain-str-model", False, graph_str) == "sonder_inference"
    assert gw_health_strings.status_calls == 1


def test_simplenamespace_mock_gateways_do_not_alias_across_gc():
    def _make_ns_graph(model: str) -> SimpleNamespace:
        ns_gw = SimpleNamespace(
            unified_worker_status=lambda: {
                "workers": [{"source": "sonder_inference_worker", "models": [model]}]
            }
        )
        return SimpleNamespace(model_gateway=ns_gw)

    for idx in range(10):
        g = _make_ns_graph(f"unique-model-{idx}:latest")
        assert legacy_chat_bridge._sonder_inference_advertises_model(g, f"unique-model-{idx}:latest") is True
        if idx > 0:
            assert legacy_chat_bridge._sonder_inference_advertises_model(g, f"unique-model-{idx - 1}:latest") is False
        del g
        gc.collect()


def test_thread_ownership_refusal_degrades_cleanly_without_hanging(monkeypatch):
    def refuse_thread(*args, **kwargs):
        raise ThreadOwnershipRefused("runtime worker admission stopped")

    monkeypatch.setattr(legacy_chat_bridge, "owned_runtime_thread", refuse_thread)
    gw = _StubGateway(models=["qwen3.6:35b"])
    graph = _graph_for(gw)

    started = time.monotonic()
    assert legacy_chat_bridge.provider_for_tier("model:qwen3.6:35b", False, graph) is None
    assert time.monotonic() - started < 0.2
    assert legacy_chat_bridge._MODEL_ADVERTISEMENT_CACHE.inflight_count == 0
    assert gw.status_calls == 0


def test_real_sonder_inference_gateway_health_dict_models_cached():
    import json
    from sonder_runtime.adapters.inference.sonder_inference_gateway import (
        SonderInferenceConfig,
        SonderInferenceGateway,
    )

    get_calls: list[str] = []

    def fake_get(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
        get_calls.append(url)
        body = json.dumps({
            "status": "ready",
            "api_version": 1,
            "models": [{"id": "qwen3.6:35b"}, {"id": "bonsai-27b:q4"}],
        }).encode("utf-8")
        return 200, body

    gw = SonderInferenceGateway(
        SonderInferenceConfig(base_url="http://127.0.0.1:11437"),
        get_transport=fake_get,
    )
    graph = _graph_for(gw)

    assert legacy_chat_bridge.provider_for_tier("model:qwen3.6:35b", False, graph) == "sonder_inference"
    assert legacy_chat_bridge.provider_for_tier("model:qwen3.6", False, graph) == "sonder_inference"
    assert legacy_chat_bridge.provider_for_tier("model:bonsai-27b:q4", False, graph) == "sonder_inference"
    assert legacy_chat_bridge.provider_for_tier("model:unknown-local", False, graph) is None
    assert len(get_calls) == 1


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1:99999",
        "http://127.0.0.1:abc",
        "https://10.77.0.2:70000/v1",
        "http://[::1",
    ],
)
def test_malformed_port_urls_do_not_raise_in_scope_key(bad_url: str):
    assert legacy_chat_bridge._safe_url_origin(bad_url) == ""
    gw = _StubGateway(models=["qwen3.6:35b"], base_url=bad_url)
    scope = legacy_chat_bridge._gateway_probe_scope(gw)
    assert scope[2] == ""
    assert legacy_chat_bridge._MODEL_ADVERTISEMENT_CACHE.advertises_model(gw, "qwen3.6:35b") is True


def test_dead_worker_thread_early_exit_does_not_strand_inflight(monkeypatch):
    """Simulate ``_ManagedThread.run()`` returning early when owner stops mid-start."""
    clock = _FakeClock()
    cache = legacy_chat_bridge._ModelAdvertisementCache(
        monotonic=clock,
        positive_ttl_seconds=30.0,
        negative_ttl_seconds=5.0,
        stale_ttl_seconds=120.0,
        wait_timeout_seconds=0.05,
    )
    gw = _StubGateway(models=["qwen3.6:35b"])

    class _EarlyExitThread(threading.Thread):
        def run(self) -> None:
            # Exits immediately without invoking target (mirrors _ManagedThread
            # when _owner._stopped flips between start() and run()).
            return None

    real_thread = legacy_chat_bridge.owned_runtime_thread
    monkeypatch.setattr(legacy_chat_bridge, "owned_runtime_thread", _EarlyExitThread)

    assert cache.advertises_model(gw, "qwen3.6:35b") is False
    assert cache.inflight_count == 0
    assert gw.status_calls == 0

    # Also verify unexpected thread-factory errors roll back cleanly.
    monkeypatch.setattr(
        legacy_chat_bridge,
        "owned_runtime_thread",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("unexpected factory error")),
    )
    clock.advance(6.0)
    assert cache.advertises_model(gw, "qwen3.6:35b") is False
    assert cache.inflight_count == 0

    # Once threads work normally again and negative TTL expires, the scope recovers.
    monkeypatch.setattr(legacy_chat_bridge, "owned_runtime_thread", real_thread)
    clock.advance(6.0)
    assert cache.advertises_model(gw, "qwen3.6:35b") is True
    assert gw.status_calls == 1
    assert cache.inflight_count == 0


def test_slotted_gateway_without_weakref_hits_warm_cache():
    class _SlottedGateway:
        __slots__ = ("models", "calls")

        def __init__(self, models: list[str]) -> None:
            self.models = models
            self.calls = 0

        def unified_worker_status(self) -> dict[str, object]:
            self.calls += 1
            return {
                "workers": [{
                    "source": "sonder_inference_worker",
                    "healthy": True,
                    "models": list(self.models),
                }],
            }

    clock = _FakeClock()
    cache = legacy_chat_bridge._ModelAdvertisementCache(
        monotonic=clock,
        positive_ttl_seconds=30.0,
        negative_ttl_seconds=5.0,
        stale_ttl_seconds=120.0,
        wait_timeout_seconds=0.5,
    )
    gw = _SlottedGateway(["qwen3.6:35b"])

    # Verify it truly cannot be weak-referenced and has no __dict__.
    assert not hasattr(gw, "__dict__")
    assert legacy_chat_bridge._gateway_identity_ref(gw) == ("none", None)

    assert cache.advertises_model(gw, "qwen3.6:35b") is True
    assert cache.advertises_model(gw, "qwen3.6:35b") is True
    assert cache.advertises_model(gw, "qwen3.6") is True
    assert cache.advertises_model(gw, "missing-model") is False
    assert gw.calls == 1


