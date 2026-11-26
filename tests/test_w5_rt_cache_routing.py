"""Wave 5 Slice 15 tests: cache-aware routing, embed/rerank, and residency admission."""
from __future__ import annotations

import pytest

from sonder_runtime.adapters import memory_rerank
from sonder_runtime.adapters.inference import gpu_admission as gpu_admission_adapter
from sonder_runtime.adapters.inference.gpu_admission_store import GpuAdmissionStore
from sonder_runtime.adapters.inference.ollama_pool import OllamaWorkerPool
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    BackendAdmissionRefused,
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    PrivateWorkerSpec,
    SonderInferenceConfig,
    SonderInferenceGateway,
    SonderInferenceOverloaded,
    SonderInferenceResponse,
    config_from_env,
    rerank_supported,
)
from sonder_runtime.application.chat.provider_bridge import UnsupportedProviderFeature
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest, RerankScore
from sonder_runtime.domain.common.errors import (
    Cancelled,
    CapacityExceeded,
    DeadlineExceeded,
    DependencyUnavailable,
    InvalidInput,
)
from sonder_runtime.domain.gpu_admission import HostMemoryReading, StreamedPlacement
from sonder_runtime.domain.routing.prefix_locality import (
    LocalityMatch,
    PrefixLocalityTable,
    score_endpoint_completion,
)
from sonder_runtime.platform.gpu_admission_config import GpuAdmissionConfig

GIB = 1024 ** 3
PRIMARY = "http://127.0.0.1:11437"
WORKER_1 = "https://192.0.2.10:8443"
WORKER_2 = "https://192.0.2.11:8443"


def _ctx(
    *,
    correlation_id: str = "w5-test",
    session_id: str | None = None,
    cancellation=None,
    cloud_allowed: bool = False,
):
    return local_owner_context(
        correlation_id=correlation_id,
        session_id=session_id,
        cancellation=cancellation,
        cloud_allowed=cloud_allowed,
    )


def _health(
    *,
    models=("qwen3:8b",),
    capabilities=("streaming",),
    features=None,
    resident_map=None,
    model_capabilities=None,
):
    model_rows = []
    for index, mid in enumerate(models):
        row: dict[str, object] = {
            "id": mid,
            "backend": "llamaserver",
            "default": index == 0,
        }
        if resident_map is not None and mid in resident_map:
            row["resident"] = resident_map[mid]
        if model_capabilities is not None and mid in model_capabilities:
            row["capabilities"] = list(model_capabilities[mid])
        model_rows.append(row)
    doc: dict[str, object] = {
        "status": "ready",
        "api_version": 1,
        "version": "0.1.0",
        "backends": [
            {
                "name": "llamaserver",
                "available": True,
                "capabilities": list(capabilities),
            }
        ],
        "models": model_rows,
        "telemetry": {
            "level": "standard",
            "subscribers": 0,
            "retained": 0,
            "capacity": 8192,
            "emitted": 0,
            "dropped": 0,
        },
    }
    if features is not None:
        doc["sonder"] = {"features": list(features)}
    return doc


def _chat_payload(
    *,
    model: str = "qwen3:8b",
    text: str = "ok",
    prompt_tokens: int = 800,
    cached_tokens: int = 0,
    predicted_tokens: int = 32,
    prompt_ms: float = 80.0,
    predicted_ms: float = 64.0,
):
    body: dict[str, object] = {
        "id": "chatcmpl-w5",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": predicted_tokens,
            "total_tokens": prompt_tokens + predicted_tokens,
            "prompt_tokens_details": {"cached_tokens": cached_tokens},
        },
        "timings": {
            "prompt_n": max(0, prompt_tokens - cached_tokens),
            "cache_n": cached_tokens,
            "predicted_n": predicted_tokens,
            "prompt_ms": prompt_ms,
            "predicted_ms": predicted_ms,
            "total_ms": prompt_ms + predicted_ms,
        },
        "sonder": {"api_version": 1, "request_id": "r-w5"},
    }
    return body


class _FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _CancelToken:
    def __init__(self, cancelled: bool = False) -> None:
        self.cancelled = cancelled

    def wait(self, timeout: float | None = None) -> bool:
        return self.cancelled


class _MultiHostStub:
    def __init__(self, health_by_host: dict[str, dict], responder) -> None:
        self.health_by_host = health_by_host
        self.responder = responder
        self.gets: list[str] = []
        self.posts: list[tuple[str, str, dict]] = []

    def get(self, url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
        import json

        for base, doc in self.health_by_host.items():
            if url.startswith(base):
                self.gets.append(url)
                return 200, json.dumps(doc).encode("utf-8")
        raise AssertionError(f"unexpected GET url: {url}")

    def post(self, url: str, payload: dict, headers: dict, timeout: float) -> dict:
        for base in self.health_by_host:
            if url.startswith(base):
                self.posts.append((base, url, payload))
                return self.responder(base, url, payload, headers)
        raise AssertionError(f"unexpected POST url: {url}")


@pytest.fixture(autouse=True)
def _clean_gpu_admission(monkeypatch):
    for key in (
        "SONDER_GPU_STREAMED_MODELS",
        "SONDER_GPU_SLOTS",
        "SONDER_ALLOW_REMOTE_INFERENCE",
        "SONDER_INFERENCE_WORKER_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    gpu_admission_adapter.reset_typed_gpu_admission()
    gpu_admission_adapter.set_process_service(None)
    yield
    gpu_admission_adapter.reset_typed_gpu_admission()
    gpu_admission_adapter.set_process_service(None)


def _worker_ca(tmp_path) -> str:
    ca_file = tmp_path / "worker-ca.pem"
    ca_file.write_text(
        "-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n",
        encoding="utf-8",
    )
    return str(ca_file)


def _streamed_gpu_service(tmp_path, *, available_bytes: int):
    cfg = GpuAdmissionConfig(
        mode="enforce",
        gpu_slots=1,
        embed_slots=1,
        on_host_floor="refuse",
        max_queue_wait_seconds=1,
    )
    return gpu_admission_adapter.GpuAdmission(
        cfg,
        store=GpuAdmissionStore(tmp_path / "gpu.sqlite3"),
        host_reader=lambda: HostMemoryReading(
            available_physical_bytes=available_bytes,
            commit_available_bytes=available_bytes,
            commit_enforced=False,
        ),
        streamed={
            "giant-moe": StreamedPlacement(
                expert_cache_bytes=12 * GIB,
                dense_bytes=4 * GIB,
                kv_bytes_per_token=1024,
                num_ctx=4096,
            ),
            "bge-large": StreamedPlacement(
                expert_cache_bytes=12 * GIB,
                dense_bytes=4 * GIB,
                kv_bytes_per_token=1024,
                num_ctx=4096,
            ),
        },
    )


# ---------------------------------------------------------------------------
# Item #17: PrefixLocalityTable + completion-cost scoring + gateway/pool
# ---------------------------------------------------------------------------


def test_prefix_locality_table_lru_decay_ttl_and_key_isolation():
    table = PrefixLocalityTable(max_entries=2, ttl_seconds=100.0, half_life_seconds=10.0)

    table.observe(
        endpoint=WORKER_1,
        model_identity="qwen3:8b",
        adapter_key="ctx=8192",
        prefix_key="pref-1",
        now=10.0,
        prompt_tokens=1000,
        cached_tokens=800,
    )
    table.observe(
        endpoint=WORKER_1,
        model_identity="qwen3:8b",
        adapter_key="ctx=8192",
        prefix_key="pref-2",
        now=11.0,
        prompt_tokens=1000,
        cached_tokens=700,
    )
    # Touch pref-1 so pref-2 becomes LRU, then observe pref-3 to evict pref-2.
    fresh_hit = table.lookup(
        endpoint=WORKER_1,
        model_identity="qwen3:8b",
        adapter_key="ctx=8192",
        prefix_key="pref-1",
        now=12.0,
        estimated_prompt_tokens=1000,
    )
    assert fresh_hit.effective_cached_tokens > 600.0

    table.observe(
        endpoint=WORKER_1,
        model_identity="qwen3:8b",
        adapter_key="ctx=8192",
        prefix_key="pref-3",
        now=13.0,
        prompt_tokens=1000,
        cached_tokens=600,
    )
    assert table.summary()["entries"] == 2
    assert table.summary()["evictions"] == 1

    # Evicted pref-2 returns miss.
    evicted = table.lookup(
        endpoint=WORKER_1,
        model_identity="qwen3:8b",
        adapter_key="ctx=8192",
        prefix_key="pref-2",
        now=14.0,
        estimated_prompt_tokens=1000,
    )
    assert evicted.effective_cached_tokens == 0.0

    # Model mismatch and adapter mismatch NEVER receive credit and record skips.
    wrong_model = table.lookup(
        endpoint=WORKER_1,
        model_identity="qwen3:32b",
        adapter_key="ctx=8192",
        prefix_key="pref-1",
        now=14.0,
        estimated_prompt_tokens=1000,
    )
    wrong_adapter = table.lookup(
        endpoint=WORKER_1,
        model_identity="qwen3:8b",
        adapter_key="ctx=32768",
        prefix_key="pref-1",
        now=14.0,
        estimated_prompt_tokens=1000,
    )
    assert wrong_model.effective_cached_tokens == 0.0
    assert wrong_adapter.effective_cached_tokens == 0.0
    assert table.summary()["incompatible_skips"] == 2

    # Recency half-life decays overlap at +10s (t=20 -> half of initial at t=10).
    decayed = table.lookup(
        endpoint=WORKER_1,
        model_identity="qwen3:8b",
        adapter_key="ctx=8192",
        prefix_key="pref-1",
        now=20.0,
        estimated_prompt_tokens=1000,
    )
    assert decayed.effective_cached_tokens == pytest.approx(400.0, abs=10.0)

    # TTL expiration at +101s returns cold zero.
    expired = table.lookup(
        endpoint=WORKER_1,
        model_identity="qwen3:8b",
        adapter_key="ctx=8192",
        prefix_key="pref-1",
        now=115.0,
        estimated_prompt_tokens=1000,
    )
    assert expired.effective_cached_tokens == 0.0


def test_prefix_locality_completion_score_balances_cache_vs_queue_saturation():
    warm_match = LocalityMatch(
        effective_cached_tokens=800.0,
        hit_ratio=0.8,
        confidence=1.0,
        decay=1.0,
        measured=True,
        age_seconds=1.0,
        estimated_restore_ms=0.0,
    )
    cold_match = LocalityMatch(0.0, 0.0, 0.0, 1.0, False, 0.0, 0.0)

    # Equal load -> warm compatible beats cold.
    warm_idle = score_endpoint_completion(
        endpoint=WORKER_1,
        inflight=0,
        max_inflight=2,
        decode_ms_per_token=2.0,
        prompt_ms_per_token=1.0,
        estimated_prompt_tokens=1000,
        expected_decode_tokens=64,
        match=warm_match,
    )
    cold_idle = score_endpoint_completion(
        endpoint=WORKER_2,
        inflight=0,
        max_inflight=2,
        decode_ms_per_token=2.0,
        prompt_ms_per_token=1.0,
        estimated_prompt_tokens=1000,
        expected_decode_tokens=64,
        match=cold_match,
    )
    assert warm_idle.warm_hit is True
    assert warm_idle.total_cost_ms < cold_idle.total_cost_ms

    # Saturated warm worker with heavy queue loses to cold idle worker.
    warm_saturated = score_endpoint_completion(
        endpoint=WORKER_1,
        inflight=3,
        max_inflight=4,
        decode_ms_per_token=2.0,
        prompt_ms_per_token=1.0,
        estimated_prompt_tokens=1000,
        expected_decode_tokens=64,
        match=warm_match,
        observed_request_ms=900.0,
    )
    assert cold_idle.total_cost_ms < warm_saturated.total_cost_ms


def test_sonder_gateway_prefix_locality_routing_prefers_compatible_warm_and_avoids_saturated_warm(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_INFERENCE", "1")
    monkeypatch.setenv("SONDER_INFERENCE_WORKER_TOKEN", "w" * 32)
    ca_path = _worker_ca(tmp_path)
    clock = _FakeClock(100.0)

    def responder(base: str, url: str, payload: dict, headers: dict) -> dict:
        assert url.endswith("/v1/chat/completions")
        cached = 1800 if base == WORKER_1 else 0
        return _chat_payload(
            model=str(payload["model"]),
            prompt_tokens=2000,
            cached_tokens=cached,
            predicted_tokens=32,
            prompt_ms=20.0 if cached else 200.0,
            predicted_ms=64.0,
        )

    hosts = _MultiHostStub(
        {
            WORKER_1: _health(models=("qwen3:8b", "qwen3:32b")),
            WORKER_2: _health(models=("qwen3:8b", "qwen3:32b")),
        },
        responder,
    )
    cfg = SonderInferenceConfig(
        base_url=PRIMARY,
        model="qwen3:8b",
        allow_remote=True,
        workers=(
            PrivateWorkerSpec(WORKER_1, ca_path, "SONDER_INFERENCE_WORKER_TOKEN", 2),
            PrivateWorkerSpec(WORKER_2, ca_path, "SONDER_INFERENCE_WORKER_TOKEN", 2),
        ),
    )
    # Force only the two private workers (primary down / skipped by not starting it -> primary refuses on health)
    hosts.health_by_host[PRIMARY] = _health(models=("other-only",))
    gw = SonderInferenceGateway(
        cfg,
        get_transport=hosts.get,
        transport=hosts.post,
        monotonic=clock,
    )
    history = (
        {"role": "system", "content": "You are a precise code assistant. " * 40},
        {"role": "user", "content": "Turn 1 context"},
        {"role": "assistant", "content": "Acknowledged"},
    )
    req_turn1 = ModelRequest(prompt="Next question", tier="code", history=history)
    r1 = gw.generate(req_turn1, _ctx(session_id="sess-a"))
    assert isinstance(r1, SonderInferenceResponse)
    # Turn 1 went to WORKER_1 (cursor 0 -> idx 0), populated locality.
    first_host = hosts.posts[0][0]
    assert first_host == WORKER_1

    # Seed WORKER_2 as also measured so unmeasured_idle doesn't hijack, then verify Turn 2
    # routes back to compatible warm WORKER_1 (even though round-robin cursor advanced).
    clock.now += 1.0
    gw._ms_per_token[WORKER_2] = 2.0
    gw._prompt_ms_per_token[WORKER_2] = 0.5
    gw._request_ewma_ms[WORKER_2] = 264.0

    r2 = gw.generate(
        ModelRequest(prompt="Follow-up", tier="code", history=history),
        _ctx(session_id="sess-a"),
    )
    assert r2.endpoint == WORKER_1
    assert hosts.posts[-1][0] == WORKER_1
    meta = gw.last_response_meta
    assert meta["routing_locality"]["warm_hit"] is True
    assert meta["routing_locality"]["effective_cached_tokens"] > 0

    status = gw.prefix_locality_status()
    assert status["summary"]["observations"] >= 2
    assert status["summary"]["hits"] >= 1
    assert "Turn 1 context" not in str(status)

    # Incompatible adapter/options (e.g. reasoning_effort) NEVER gets warm credit:
    # make WORKER_2 intrinsically faster so loss of locality credit switches to WORKER_2.
    gw._ms_per_token[WORKER_1] = 2.0
    gw._prompt_ms_per_token[WORKER_1] = 0.5
    gw._request_ewma_ms[WORKER_1] = 300.0
    gw._ms_per_token[WORKER_2] = 1.0
    gw._prompt_ms_per_token[WORKER_2] = 0.2
    gw._request_ewma_ms[WORKER_2] = 150.0
    r_mismatch = gw.generate(
        ModelRequest(
            prompt="Structured call",
            tier="code",
            history=history,
            options={"reasoning_effort": "off"},
        ),
        _ctx(session_id="sess-a"),
    )
    assert isinstance(r_mismatch, SonderInferenceResponse)
    assert gw.last_response_meta["routing_locality"]["warm_hit"] is False
    assert gw.prefix_locality_status()["summary"]["incompatible_skips"] >= 1
    assert r_mismatch.endpoint == WORKER_2

    # Saturated compatible warm worker (inflight=1, slow EWMA) loses to cold idle WORKER_2.
    with gw._cond:
        gw._inflight[WORKER_1] = 1
        gw._request_ewma_ms[WORKER_1] = 5000.0
        gw._request_ewma_ms[WORKER_2] = 120.0
        gw._ms_per_token[WORKER_1] = 4.0
        gw._ms_per_token[WORKER_2] = 2.0
    try:
        r_sat = gw.generate(
            ModelRequest(prompt="While worker 1 is busy", tier="code", history=history),
            _ctx(session_id="sess-a"),
        )
        assert r_sat.endpoint == WORKER_2
    finally:
        with gw._cond:
            gw._inflight.pop(WORKER_1, None)


def test_ollama_pool_prefix_locality_routing_and_saturation_and_incompatibility():
    clock = _FakeClock(200.0)
    pool = OllamaWorkerPool(
        "http://127.0.0.1:11434",
        ("http://127.0.0.1:11435",),
        max_inflight_per_worker=2,
        capability_prober=lambda origin: {"models": ["qwen3:8b"], "latency_ms": 10.0},
        time_fn=clock,
        locality_max_entries=8,
    )
    pool.refresh_capabilities()
    seen_origins: list[str] = []
    chat_payload = {
        "model": "qwen3:8b",
        "messages": [
            {"role": "system", "content": "Shared repository instructions " * 30},
            {"role": "user", "content": "Prior context"},
            {"role": "assistant", "content": "Prior answer"},
            {"role": "user", "content": "Current turn"},
        ],
        "options": {"num_ctx": 8192},
    }

    def send(origin: str):
        seen_origins.append(origin)
        cached = 900 if origin == "http://127.0.0.1:11434" else 0
        return {
            "prompt_eval_count": 1000,
            "prompt_eval_cached_count": cached,
            "prompt_eval_duration": 20_000_000 if cached else 200_000_000,
            "eval_count": 40,
            "eval_duration": 80_000_000,
        }

    # Turn 1 runs on worker 0 (11434) and populates prefix locality.
    pool.request(send, model="qwen3:8b", payload=chat_payload, prefix_key="conv-1")
    first = seen_origins[-1]
    assert first == "http://127.0.0.1:11434"

    # Turn 2 with same prefix_key + model + num_ctx chooses warm worker 0 even after cursor advanced.
    clock.now += 1.0
    pool.request(send, model="qwen3:8b", payload=chat_payload, prefix_key="conv-1")
    assert seen_origins[-1] == "http://127.0.0.1:11434"

    # Mismatched num_ctx / adapter does not hit warm cache and increments incompatible_skips.
    mismatched_payload = {
        "model": "qwen3:8b",
        "messages": chat_payload["messages"],
        "options": {"num_ctx": 32768},
    }
    pool.request(send, model="qwen3:8b", payload=mismatched_payload, prefix_key="conv-1")
    loc_status = pool.status()["prefix_locality"]
    assert loc_status["summary"]["incompatible_skips"] >= 1
    assert pool.status()["routing"] == "latency-aware-least-inflight"
    assert "prefix_locality" not in pool.summary()

    # Saturated warm worker 0 loses to idle worker 1 (11435).
    with pool._condition:
        w0, w1 = pool._states
        w0.inflight = 1
        w0.inference_ewma_ms = 5000.0
        w1.inflight = 0
        w1.inference_ewma_ms = 120.0
    try:
        pool.request(send, model="qwen3:8b", payload=chat_payload, prefix_key="conv-1")
        assert seen_origins[-1] == "http://127.0.0.1:11435"
    finally:
        with pool._condition:
            w0.inflight = 0


# ---------------------------------------------------------------------------
# Item #1: Tightened SonderInferenceGateway.embed()
# ---------------------------------------------------------------------------


def test_embed_explicit_model_switch_validation_cancellation_and_admission(
    tmp_path, monkeypatch
):
    # Env config picks up explicit embed & rerank models.
    env_cfg = config_from_env(
        {
            "SONDER_INFERENCE_EMBED_MODEL": "bge-large",
            "SONDER_INFERENCE_RERANK_MODEL": "bge-reranker",
        }
    )
    assert env_cfg.embed_model == "bge-large"
    assert env_cfg.rerank_model == "bge-reranker"

    health = _health(
        models=("bge-small", "bge-large", "chat-only"),
        capabilities=("streaming",),
        model_capabilities={
            "bge-small": ("embeddings",),
            "bge-large": ("embeddings",),
            "chat-only": ("streaming",),
        },
        resident_map={"bge-large": False},
    )

    posts: list[tuple[str, dict]] = []
    next_response: list[dict] = []

    def fake_get(url: str, headers: dict, timeout: float):
        import json

        return 200, json.dumps(health).encode("utf-8")

    def fake_post(url: str, payload: dict, headers: dict, timeout: float):
        posts.append((url, payload))
        return next_response.pop(0)

    gw = SonderInferenceGateway(
        SonderInferenceConfig(base_url=PRIMARY, model="bge-small"),
        get_transport=fake_get,
        transport=fake_post,
    )

    # Pre-I/O rejections (str/bytes/empty/bad explicit model) -> 0 transport calls
    for bad in ("not-a-list", b"bytes", [], [1], ["x"] * 257):
        with pytest.raises(InvalidInput):
            gw.embed(bad, _ctx())  # type: ignore[arg-type]
    with pytest.raises(InvalidInput):
        gw.embed(["ok"], _ctx(), model="   ")
    assert posts == []

    # Per-model capability gating refuses chat-only explicit model before POST.
    with pytest.raises(DependencyUnavailable, match="SONDER_EMBEDDING_PROVIDER=ollama"):
        gw.embed(["alpha"], _ctx(), model="chat-only")
    assert posts == []

    # Already-cancelled operation refuses before any transport call.
    with pytest.raises(Cancelled):
        gw.embed(["alpha"], _ctx(cancellation=_CancelToken(True)), model="bge-large")
    assert posts == []

    # Mid-call cancellation converts a post-cancel transport failure into Cancelled.
    mid_token = _CancelToken(False)

    def cancelling_post(url: str, payload: dict, headers: dict, timeout: float):
        mid_token.cancelled = True
        raise ConnectionResetError("aborted on cancel")

    mid_gw = SonderInferenceGateway(
        SonderInferenceConfig(base_url=PRIMARY, model="bge-small"),
        get_transport=fake_get,
        transport=cancelling_post,
    )
    with pytest.raises(Cancelled):
        mid_gw.embed(["alpha"], _ctx(cancellation=mid_token), model="bge-large")

    # Explicit model switch sends requested model and preserves served identity.
    next_response.append(
        {
            "object": "list",
            "model": "bge-large",
            "data": [
                {"index": 1, "embedding": [0.2, 0.3]},
                {"index": 0, "embedding": [0.0, 0.1]},
            ],
        }
    )
    vecs = gw.embed(["first", "second"], _ctx(), model="bge-large")
    assert posts[-1][1] == {"model": "bge-large", "input": ["first", "second"]}
    assert [(v.vector, v.model) for v in vecs] == [
        ((0.0, 0.1), "bge-large"),
        ((0.2, 0.3), "bge-large"),
    ]

    # Served-model identity preservation across dynamic model switches without stale caching.
    gw_default = SonderInferenceGateway(
        SonderInferenceConfig(base_url=PRIMARY, model="default"),
        get_transport=fake_get,
        transport=fake_post,
    )
    next_response.append(
        {
            "object": "list",
            "model": "bge-small",
            "data": [{"index": 0, "embedding": [1.0, 2.0]}],
        }
    )
    assert gw_default.embed(["a"], _ctx())[0].model == "bge-small"
    next_response.append(
        {
            "object": "list",
            "model": "bge-large",
            "data": [{"index": 0, "embedding": [3.0, 4.0]}],
        }
    )
    assert gw_default.embed(["b"], _ctx())[0].model == "bge-large"

    # Strict served-model rejections: "default", missing/blank, or explicit mismatch.
    for bad_resp in (
        {"object": "list", "model": "default", "data": [{"index": 0, "embedding": [1.0]}]},
        {"object": "list", "data": [{"index": 0, "embedding": [1.0]}]},
        {"object": "list", "model": "bge-small", "data": [{"index": 0, "embedding": [1.0]}]},
        {
            "object": "list",
            "model": "bge-large",
            "data": [
                {"index": True, "embedding": [1.0]},
            ],
        },
        {
            "object": "list",
            "model": "bge-large",
            "data": [
                {"index": 0, "embedding": [1.0, 2.0]},
                {"index": 1, "embedding": [3.0]},
            ],
        },
        {"object": "list", "model": "bge-large", "data": "not-a-list"},
    ):
        next_response.append(bad_resp)
        with pytest.raises(DependencyUnavailable):
            gw.embed(
                ["a"] * (2 if isinstance(bad_resp.get("data"), list) and len(bad_resp["data"]) == 2 else 1),
                _ctx(),
                model="bge-large",
            )

    # Local GPU AdmissionLane.EMBED hook: cold model refused on low host RAM; resident passes.
    monkeypatch.setenv("SONDER_GPU_STREAMED_MODELS", "{}")
    service = _streamed_gpu_service(tmp_path, available_bytes=1 * GIB)
    gpu_admission_adapter.set_process_service(service)

    before_posts = len(posts)
    with pytest.raises(BackendAdmissionRefused) as caught:
        gw.embed(["blocked cold embed"], _ctx(), model="bge-large")
    assert caught.value.reason == "host_ram_floor"
    assert len(posts) == before_posts  # refused BEFORE /v1/embeddings POST

    # Flip /v1/sonder/health resident=True and refresh cache -> succeeds under identical low RAM.
    health["models"][1]["resident"] = True
    gw.health(refresh=True)
    next_response.append(
        {
            "object": "list",
            "model": "bge-large",
            "data": [{"index": 0, "embedding": [0.9, 0.8]}],
        }
    )
    res = gw.embed(["resident embed"], _ctx(), model="bge-large")
    assert res[0].model == "bge-large"


# ---------------------------------------------------------------------------
# Item #2: Cross-encoder /v1/rerank client & memory_rerank.py fallback
# ---------------------------------------------------------------------------


def test_rerank_capability_gating_batching_order_and_memory_rerank_fallback():
    plain_health = _health(models=("reranker",), capabilities=("chat",))
    assert rerank_supported(plain_health) is False

    rerank_health = _health(
        models=("bge-reranker",),
        capabilities=("chat",),
        features=("rerank",),
    )
    assert rerank_supported(rerank_health) is True

    posts: list[tuple[str, dict]] = []

    def fake_get_plain(url: str, headers: dict, timeout: float):
        import json

        return 200, json.dumps(plain_health).encode("utf-8")

    def fake_get_rerank(url: str, headers: dict, timeout: float):
        import json

        return 200, json.dumps(rerank_health).encode("utf-8")

    # Unadvertised rerank fails with UnsupportedProviderFeature before any POST.
    plain_gw = SonderInferenceGateway(
        SonderInferenceConfig(base_url=PRIMARY, model="bge-reranker"),
        get_transport=fake_get_plain,
        transport=lambda url, payload, headers, timeout: posts.append((url, payload)) or {},
    )
    assert plain_gw.supports_rerank() is False
    with pytest.raises(UnsupportedProviderFeature):
        plain_gw.rerank("query", ["doc a", "doc b"], _ctx())
    assert posts == []

    # Pre-I/O input validation before any network call.
    def rerank_post(url: str, payload: dict, headers: dict, timeout: float):
        posts.append((url, payload))
        docs = payload["documents"]
        score_map = {
            "d0": 0.1,
            "d1": 0.95,
            "d2": -0.4,
            "d3": 0.85,
            "d4": 0.85,
        }
        # Deliberately return rows in reversed index order to test realignment.
        return {
            "model": "bge-reranker",
            "results": [
                {"index": idx, "relevance_score": score_map[docs[idx]]}
                for idx in reversed(range(len(docs)))
            ],
        }

    gw = SonderInferenceGateway(
        SonderInferenceConfig(base_url=PRIMARY, model="bge-reranker"),
        get_transport=fake_get_rerank,
        transport=rerank_post,
    )
    assert gw.supports_rerank() is True
    for bad_docs in ("single", b"bytes", [], ["ok", 42]):
        with pytest.raises(InvalidInput):
            gw.rerank("q", bad_docs, _ctx())  # type: ignore[arg-type]
    with pytest.raises(InvalidInput):
        gw.rerank(" ", ["d0"], _ctx())
    with pytest.raises(InvalidInput):
        gw.rerank("q", ["d0"], _ctx(), top_n=0)
    with pytest.raises(InvalidInput):
        gw.rerank("q", ["d0"], _ctx(), batch_size=999)
    assert posts == []

    # Bounded batching (5 documents, batch_size=2 -> 3 POSTs) with global top_n & tie breaking.
    scored = gw.rerank(
        "find top docs",
        ["d0", "d1", "d2", "d3", "d4"],
        _ctx(),
        batch_size=2,
        top_n=3,
        return_documents=True,
    )
    assert len(posts) == 3
    assert [len(call[1]["documents"]) for call in posts] == [2, 2, 1]
    assert scored == (
        RerankScore(index=1, relevance_score=0.95, model="bge-reranker", document="d1"),
        RerankScore(index=3, relevance_score=0.85, model="bge-reranker", document="d3"),
        RerankScore(index=4, relevance_score=0.85, model="bge-reranker", document="d4"),
    )

    # memory_rerank integration: default MMR works without reranker, uses reranker
    # when healthy, falls back cleanly on unavailability/timeout, and propagates Cancelled.
    candidates = [
        ("d0", [1.0, 0.0]),
        ("d1", [0.2, 0.9]),
        ("d2", [0.9, 0.1]),
    ]
    texts = ["d0", "d1", "d2"]
    ctx = _ctx()
    default_mmr = memory_rerank.rerank_candidates(
        [1.0, 0.0],
        candidates,
        k=2,
        query_text="find top docs",
        candidate_texts=texts,
        context=ctx,
    )
    assert default_mmr == ["d0", "d1"]

    neural_picked = memory_rerank.rerank_candidates(
        [1.0, 0.0],
        candidates,
        k=2,
        query_text="find top docs",
        candidate_texts=texts,
        reranker=gw,
        context=ctx,
    )
    assert neural_picked == ["d1", "d0"]

    class _UnavailableReranker:
        def rerank(self, query, documents, context=None, **kwargs):
            raise DependencyUnavailable("reranker down")

    fallback_mmr = memory_rerank.rerank_candidates(
        [1.0, 0.0],
        candidates,
        k=2,
        query_text="find top docs",
        candidate_texts=texts,
        reranker=_UnavailableReranker(),
        context=ctx,
    )
    assert fallback_mmr == ["d0", "d1"]

    for exc_cls in (CapacityExceeded, DeadlineExceeded, UnsupportedProviderFeature):
        class _DegradedReranker:
            def __init__(self, err_cls=exc_cls):
                self._err_cls = err_cls

            def rerank(self, query, documents, context=None, **kwargs):
                raise self._err_cls("degraded")

        assert memory_rerank.rerank_candidates(
            [1.0, 0.0],
            candidates,
            k=2,
            query_text="find top docs",
            candidate_texts=texts,
            reranker=_DegradedReranker(),
            context=ctx,
        ) == ["d0", "d1"]

    class _CancelReranker:
        def rerank(self, query, documents, context=None, **kwargs):
            raise Cancelled("stopped")

    with pytest.raises(Cancelled):
        memory_rerank.rerank_candidates(
            [1.0, 0.0],
            candidates,
            k=2,
            query_text="find top docs",
            candidate_texts=texts,
            reranker=_CancelReranker(),
            context=ctx,
        )


def test_rerank_worker_failover_and_mid_batch_model_drift(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_INFERENCE", "1")
    monkeypatch.setenv("SONDER_INFERENCE_WORKER_TOKEN", "w" * 32)
    ca_path = _worker_ca(tmp_path)

    call_count = {"w2": 0}

    def responder(base: str, url: str, payload: dict, headers: dict) -> dict:
        if base == WORKER_1:
            raise SonderInferenceOverloaded("busy")
        call_count["w2"] += 1
        return {
            "model": "bge-reranker" if call_count["w2"] == 1 else "other-reranker",
            "results": [
                {"index": idx, "relevance_score": 1.0 - idx * 0.1}
                for idx in range(len(payload["documents"]))
            ],
        }

    hosts = _MultiHostStub(
        {
            PRIMARY: _health(models=("other-only",), capabilities=("chat",)),
            WORKER_1: _health(models=("bge-reranker",), features=("rerank",)),
            WORKER_2: _health(models=("bge-reranker",), features=("rerank",)),
        },
        responder,
    )
    gw = SonderInferenceGateway(
        SonderInferenceConfig(
            base_url=PRIMARY,
            model="bge-reranker",
            allow_remote=True,
            workers=(
                PrivateWorkerSpec(WORKER_1, ca_path, "SONDER_INFERENCE_WORKER_TOKEN", 1),
                PrivateWorkerSpec(WORKER_2, ca_path, "SONDER_INFERENCE_WORKER_TOKEN", 1),
            ),
        ),
        get_transport=hosts.get,
        transport=hosts.post,
    )
    # Single-batch succeeds on WORKER_2 after WORKER_1 overload.
    scored = gw.rerank("q", ["a", "b"], _ctx(), batch_size=4)
    assert [s.model for s in scored] == ["bge-reranker", "bge-reranker"]
    assert call_count["w2"] == 1

    # Multi-batch on WORKER_2 fails closed when served model drifts mid-request.
    call_count["w2"] = 0
    with pytest.raises(DependencyUnavailable, match="switched rerank served model"):
        gw.rerank("q", ["a", "b", "c"], _ctx(), batch_size=2)


# ---------------------------------------------------------------------------
# Item #18: OpenAICompatibleGateway residency_probe / is_model_resident seam
# ---------------------------------------------------------------------------


def test_openai_compat_residency_probe_distinguishes_resident_cold_and_unknown(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SONDER_GPU_STREAMED_MODELS", "{}")
    service = _streamed_gpu_service(tmp_path, available_bytes=1 * GIB)
    gpu_admission_adapter.set_process_service(service)

    posts: list[tuple[str, str]] = []
    cfg = OpenAICompatibleConfig(base_url="http://127.0.0.1:8081", model="giant-moe")

    # 1. Cold load (residency_probe -> False) refuses BEFORE transport with host_ram_floor.
    cold_gw = OpenAICompatibleGateway(
        cfg,
        transport=lambda url, payload, headers, timeout: posts.append(("cold", url)) or {
            "choices": [{"message": {"content": "ok"}}],
        },
        residency_probe=lambda model, base_url: False,
    )
    with pytest.raises(BackendAdmissionRefused) as caught:
        cold_gw.generate(ModelRequest(prompt="hi", tier="code"), _ctx())
    assert caught.value.reason == "host_ram_floor"
    assert posts == []

    # 2. Warm resident model (is_model_resident alias -> True) bypasses host-RAM refusal.
    resident_gw = OpenAICompatibleGateway(
        cfg,
        transport=lambda url, payload, headers, timeout: posts.append(("warm", url)) or {
            "choices": [{"message": {"content": "warm-ok"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2},
        },
        is_model_resident=lambda model: True,
    )
    assert resident_gw.residency_probe is not None
    resp = resident_gw.generate(ModelRequest(prompt="hi", tier="code"), _ctx())
    assert resp.text == "warm-ok"
    assert posts == [("warm", "http://127.0.0.1:8081/v1/chat/completions")]

    # 3. Unknown / raising residency probe (None or error) queues slot without RAM refusal.
    unknown_gw = OpenAICompatibleGateway(
        cfg,
        transport=lambda url, payload, headers, timeout: posts.append(("unknown", url)) or {
            "choices": [{"message": {"content": "unknown-ok"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2},
        },
        residency_probe=lambda: None,
    )
    assert unknown_gw.generate(ModelRequest(prompt="hi", tier="code"), _ctx()).text == "unknown-ok"

    # 4. SonderInferenceGateway exposes health-backed default residency check cleanly.
    health = _health(
        models=("giant-moe",),
        capabilities=("streaming",),
        resident_map={"giant-moe": False},
    )
    sgw = SonderInferenceGateway(
        SonderInferenceConfig(base_url="http://127.0.0.1:8081", model="giant-moe"),
        get_transport=lambda url, headers, timeout: (
            200,
            __import__("json").dumps(health).encode("utf-8"),
        ),
        transport=lambda url, payload, headers, timeout: posts.append(("sgw", url)) or _chat_payload(
            model="giant-moe",
        ),
    )
    with pytest.raises(BackendAdmissionRefused) as sgw_caught:
        sgw.generate(ModelRequest(prompt="cold streamed", tier="code"), _ctx())
    assert sgw_caught.value.reason == "host_ram_floor"

    health["models"][0]["resident"] = True
    sgw.health(refresh=True)
    ok_sgw = sgw.generate(ModelRequest(prompt="resident streamed", tier="code"), _ctx())
    assert ok_sgw.text == "ok"
