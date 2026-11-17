"""Tests for multi-node pool capability discovery, scoring, and context policy."""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from sonder_runtime.adapters.inference.ollama_pool import (
    OllamaWorkerPool,
    WorkerCapabilities,
    _default_capability_prober,
)
from sonder_runtime.adapters.inference.pool_model_catalog import pool_model_capabilities
from sonder_runtime.adapters.inference.residency_feedback import (
    ResidencyFeedback,
    record_dispatched_context,
)
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceConfig,
    SonderInferenceGateway,
)
from sonder_runtime.platform import context_policy
from sonder_runtime.platform.environment_options import local_model_options

PRIMARY = "http://127.0.0.1:11434"
NODE1 = "https://10.77.0.2:8443"
NODE2 = "https://10.77.0.3:8443"


def _json_response(payload: dict) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


# ---------------------------------------------------------------------------
# _default_capability_prober + lazy /api/show caching + /api/ps
# ---------------------------------------------------------------------------


def test_default_capability_prober_reads_ps_and_caches_show_by_digest():
    show_calls: list[tuple[str, str]] = []
    digest = "sha256:aaa111"

    def fake_open_url(request, **_kwargs):
        url = request.full_url
        if url.endswith("/api/version"):
            return _json_response({
                "version": "0.40.16",
                "kv_cache_type": "q8_0",
                "vram_bytes": 32 * (1024 ** 3),
                "ram_bytes": 64 * (1024 ** 3),
            })
        if url.endswith("/api/tags"):
            return _json_response({
                "models": [
                    {
                        "name": "astra-cyber-35b-a3b:latest",
                        "digest": digest,
                        "details": {"parameter_size": "35B"},
                    },
                ]
            })
        if url.endswith("/api/ps"):
            return _json_response({
                "models": [
                    {
                        "name": "astra-cyber-35b-a3b:latest",
                        "size": 21_000_000_000,
                        "size_vram": 21_000_000_000,
                        "context_length": 32768,
                    }
                ]
            })
        if url.endswith("/api/show"):
            body = json.loads(request.data.decode("utf-8"))
            show_calls.append((url, body["model"]))
            return _json_response({
                "parameters": "num_ctx 32768\ntemperature 0.6",
                "details": {"parameter_size": "35B"},
                "model_info": {
                    "qwen35moe.context_length": 262144,
                    "qwen35moe.expert_count": 256,
                    "qwen35moe.expert_used_count": 8,
                },
                "capabilities": ["completion", "tools", "thinking"],
            })
        raise AssertionError(f"unexpected URL {url}")

    probe = _default_capability_prober(allow_remote=True, open_url=fake_open_url)
    first = probe(NODE1)
    assert first["version"] == "0.40.16"
    assert first["models"] == ("astra-cyber-35b-a3b:latest",)
    assert first["loaded_models"] == ("astra-cyber-35b-a3b:latest",)
    # /api/ps supplies active context and tag name supplies MoE hint without eager /api/show.
    assert first["model_contexts"]["astra-cyber-35b-a3b:latest"] == 32768
    assert first["model_active_billions"]["astra-cyber-35b-a3b:latest"] == 3.0
    assert first["kv_cache_type"] == "q8_0"
    assert first["vram_bytes"] == 32 * (1024 ** 3)
    assert first["ram_bytes"] == 64 * (1024 ** 3)
    assert len(show_calls) == 0

    # Lazy /api/show fetches once per (origin, model, digest) and populates cache.
    details = probe.show(NODE1, "astra-cyber-35b-a3b:latest", digest=digest)
    assert details["context_length"] == 262144
    assert details["supports_tools"] is True
    assert details["active_billions"] == 3.0
    assert len(show_calls) == 1

    # Subsequent probe(NODE1) and show() calls with same digest reuse cached /api/show.
    second = probe(NODE1)
    assert second["model_contexts"]["astra-cyber-35b-a3b:latest"] == 262144
    assert second["supports_tools"]["astra-cyber-35b-a3b:latest"] is True
    probe.show(NODE1, "astra-cyber-35b-a3b:latest", digest=digest)
    assert len(show_calls) == 1

    # Changing the model digest invalidates cache and triggers a fresh /api/show lookup.
    digest = "sha256:bbb222"
    probe.show(NODE1, "astra-cyber-35b-a3b:latest", digest=digest)
    assert len(show_calls) == 2


def test_default_capability_prober_degrades_gracefully_when_ps_and_show_fail():
    def fake_open_url(request, **_kwargs):
        url = request.full_url
        if url.endswith("/api/version"):
            return _json_response({"version": "0.39.0"})
        if url.endswith("/api/tags"):
            return _json_response({"models": [{"name": "llama3:8b"}]})
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    probe = _default_capability_prober(allow_remote=False, open_url=fake_open_url)
    report = probe(PRIMARY)
    assert report == {
        "protocol": "ollama-http-v1",
        "version": "0.39.0",
        "models": ("llama3:8b",),
    }


# ---------------------------------------------------------------------------
# Dynamic register_worker + scoring + aggregate budgets
# ---------------------------------------------------------------------------


def test_register_worker_connects_and_pools_node1_dynamically():
    probes = {
        PRIMARY: {
            "protocol": "ollama-http-v1",
            "version": "0.40.16",
            "models": ["qwen3.5:9b"],
            "loaded_models": ["qwen3.5:9b"],
            "model_contexts": {"qwen3.5:9b": 32768},
            "vram_bytes": 16 * (1024 ** 3),
            "kv_cache_type": "f16",
            "supports_tools": {"qwen3.5:9b": True},
        },
        NODE1: {
            "protocol": "ollama-http-v1",
            "version": "0.40.16",
            "models": ["astra-cyber-35b-a3b:latest", "qwen3.5:9b"],
            "loaded_models": ["astra-cyber-35b-a3b:latest"],
            "model_contexts": {
                "astra-cyber-35b-a3b:latest": 32768,
                "qwen3.5:9b": 65536,
            },
            "vram_bytes": 32 * (1024 ** 3),
            "kv_cache_type": "q8_0",
            "supports_tools": {"astra-cyber-35b-a3b:latest": True, "qwen3.5:9b": True},
            "model_active_billions": {"astra-cyber-35b-a3b:latest": 3.0},
        },
    }
    pool = OllamaWorkerPool(
        PRIMARY,
        capability_prober=lambda origin: probes[origin],
    )
    pool.refresh_capabilities(force=True)
    assert pool.origins == (PRIMARY,)

    with pytest.raises(ValueError, match="https"):
        pool.register_worker("http://10.77.0.2:11434", allow_remote=True)

    snap = pool.register_worker(NODE1, allow_remote=True, refresh=True)
    assert snap.origin == NODE1
    assert snap.healthy is True
    assert snap.kv_cache_type == "q8_0"
    assert snap.vram_bytes == 32 * (1024 ** 3)
    assert snap.model_contexts["astra-cyber-35b-a3b:latest"] == 32768
    assert pool.origins == (PRIMARY, NODE1)
    assert pool.enabled is True

    # Idempotent re-registration refreshes in place without duplicating workers.
    pool.register_worker(NODE1 + "/", allow_remote=True, refresh=False)
    assert pool.origins == (PRIMARY, NODE1)

    budget = pool.aggregate_context_budget("astra-cyber-35b-a3b:latest")
    assert budget["eligible_workers"] == 1
    assert budget["max_context"] == 32768
    assert budget["recommended_worker_origin"] == NODE1
    assert budget["resident_origins"] == [NODE1]
    assert budget["supports_tools"] is True
    assert budget["active_billions"] == 3.0

    capacity = pool.pool_capacity_summary()
    assert capacity["worker_count"] == 2
    assert capacity["healthy_worker_count"] == 2
    assert capacity["total_vram_bytes"] == 48 * (1024 ** 3)
    assert capacity["model_contexts"]["qwen3.5:9b"] == 65536

    catalog_caps = pool_model_capabilities(pool)
    assert catalog_caps["astra-cyber-35b-a3b:latest"]["resident_origins"] == [NODE1]
    assert catalog_caps["qwen3.5:9b"]["origins"] == [PRIMARY, NODE1]
    assert catalog_caps["qwen3.5:9b"]["max_context"] == 65536


def test_choose_prefers_resident_model_and_sufficient_context_headroom():
    reports = {
        PRIMARY: {
            "protocol": "ollama-http-v1",
            "version": "0.40.16",
            "models": ["coder:35b"],
            "loaded_models": [],
            "model_contexts": {"coder:35b": 8192},
            "latency_ms": 5.0,
        },
        NODE1: {
            "protocol": "ollama-http-v1",
            "version": "0.40.16",
            "models": ["coder:35b"],
            "loaded_models": ["coder:35b"],
            "model_contexts": {"coder:35b": 32768},
            "latency_ms": 40.0,
        },
        NODE2: {
            "protocol": "ollama-http-v1",
            "version": "0.40.16",
            "models": ["coder:35b"],
            "loaded_models": [],
            "model_contexts": {"coder:35b": 65536},
            "latency_ms": 20.0,
        },
    }
    pool = OllamaWorkerPool(
        PRIMARY,
        (NODE1, NODE2),
        allow_remote=True,
        capability_prober=lambda origin: reports[origin],
    )
    pool.refresh_capabilities(force=True)

    chosen: list[str] = []
    # Even though PRIMARY has 5ms latency and NODE2 has 20ms, NODE1 has coder:35b resident.
    pool.request(lambda origin: chosen.append(origin) or "ok", model="coder:35b")
    assert chosen[-1] == NODE1

    # Now make nothing resident and ask for required_num_ctx=32768: NODE2 wins over faster PRIMARY (8192).
    reports[NODE1]["loaded_models"] = []
    reports[NODE1]["model_contexts"]["coder:35b"] = 16384
    pool.refresh_capabilities(force=True)
    chosen.clear()
    pool.request(
        lambda origin: chosen.append(origin) or "ok",
        model="coder:35b",
        payload={"options": {"num_ctx": 32768}},
    )
    assert chosen[-1] == NODE2


def test_worker_capabilities_backward_compatible_construction():
    caps = WorkerCapabilities("ollama", "0.40.0", ("m:7b",), 100.0, 2)
    assert caps.model_contexts == {}
    assert caps.loaded_models == ()
    assert caps.vram_bytes is None
    assert caps.context_for("m:7b") is None
    assert caps.model_supports_tools("m:7b") is None
    assert caps.active_billions_for("m:7b") is None


# ---------------------------------------------------------------------------
# context_policy MoE active_billions & node VRAM / KV cache
# ---------------------------------------------------------------------------


def test_context_policy_uses_moe_active_billions_and_node_capabilities(monkeypatch):
    monkeypatch.delenv("SONDER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SONDER_SESSION_NUM_CTX", raising=False)
    monkeypatch.delenv("SONDER_KV_CACHE_TYPE", raising=False)
    monkeypatch.delenv("OLLAMA_KV_CACHE_TYPE", raising=False)

    # Dense 35B on 16 GiB f16 gets clamped by parameters>=24B -> 16384, capped at fp16 default 8192.
    dense_q8 = context_policy.auto_context_plan(
        262144, "35B", node_kv_cache_type="q8_0",
    )
    assert dense_q8["context"] == 16384
    assert dense_q8["kv_cache_source"] == "node-capability"
    assert "parameters>=24B" in dense_q8["clamps"]

    # Same 35B total size with MoE active_billions=3.0 on Node1 q8_0 + 32 GiB VRAM keeps full 32768.
    moe_plan = context_policy.auto_context_plan(
        262144,
        "35B",
        active_billions=3.0,
        node_vram_bytes=32 * (1024 ** 3),
        node_kv_cache_type="q8_0",
    )
    assert moe_plan["context"] == 32768
    assert moe_plan["active_billions"] == 3.0
    assert moe_plan["node_vram_bytes"] == 32 * (1024 ** 3)
    assert "parameters>=24B" not in moe_plan["clamps"]

    # Operator env pin still overrides automatic tuning.
    monkeypatch.setenv("SONDER_CONTEXT_SIZE", "12k")
    assert context_policy.auto_context(
        262144, "35B", active_billions=3.0, node_kv_cache_type="q8_0",
    ) == 12000


# ---------------------------------------------------------------------------
# ResidencyFeedback multi-origin keying & environment_options remote thread
# ---------------------------------------------------------------------------


def test_residency_feedback_isolates_ceilings_by_origin():
    ps_by_origin = {
        PRIMARY: {
            "models": [{"name": "qwen3.6:35b", "size": 20_000_000_000, "size_vram": 15_000_000_000}],
        },
        NODE1: {
            "models": [{"name": "qwen3.6:35b", "size": 20_000_000_000, "size_vram": 20_000_000_000}],
        },
    }
    now = [100.0]
    feedback = ResidencyFeedback(
        lambda: ps_by_origin[PRIMARY],
        fetch_ps_for_origin=lambda origin: ps_by_origin[origin],
        minimum_context=512,
        origin=PRIMARY,
        clock=lambda: now[0],
        check_interval=0,
    )

    record_dispatched_context(
        "/api/chat",
        {"model": "qwen3.6:35b", "options": {"num_ctx": 16384}},
        lambda: feedback,
        lambda _m: False,
        origin=PRIMARY,
    )
    record_dispatched_context(
        "/api/chat",
        {"model": "qwen3.6:35b", "options": {"num_ctx": 32768}},
        lambda: feedback,
        lambda _m: False,
        origin=NODE1,
    )

    primary_verdict = feedback.refresh("qwen3.6:35b", geometry=None, kv_type="f16", origin=PRIMARY)
    node1_verdict = feedback.refresh("qwen3.6:35b", geometry=None, kv_type="q8_0", origin=NODE1)
    assert primary_verdict is not None and primary_verdict.placement == "gpu+ram-hybrid"
    assert node1_verdict is not None and node1_verdict.placement == "gpu-resident"
    assert feedback.ceiling("qwen3.6:35b", origin=PRIMARY) == 8192
    assert feedback.ceiling("qwen3.6:35b", origin=NODE1) is None


def test_environment_options_omits_coordinator_threads_for_remote_worker():
    local_opts = local_model_options(0.2, 256, 8192, environ={})
    assert "num_thread" in local_opts

    remote_opts = local_model_options(0.2, 256, 8192, environ={}, remote=True)
    assert "num_thread" not in remote_opts
    assert remote_opts["num_ctx"] == 8192

    pinned_remote = local_model_options(
        0.2, 256, 8192, environ={"SONDER_NUM_THREAD": "6"}, remote=True,
    )
    assert pinned_remote["num_thread"] == 6


# ---------------------------------------------------------------------------
# SonderInferenceGateway unified OLLAMA_POOL discovery/health
# ---------------------------------------------------------------------------


def test_sonder_inference_gateway_unifies_ollama_pool_snapshots():
    probes = {
        PRIMARY: {
            "protocol": "ollama-http-v1",
            "version": "0.40.16",
            "models": ["sonder:latest"],
            "loaded_models": ["sonder:latest"],
            "model_contexts": {"sonder:latest": 32768},
            "supports_tools": {"sonder:latest": True},
            "vram_bytes": 16 * (1024 ** 3),
        },
        NODE1: {
            "protocol": "ollama-http-v1",
            "version": "0.40.16",
            "models": ["astra-cyber-35b-a3b:latest"],
            "loaded_models": ["astra-cyber-35b-a3b:latest"],
            "model_contexts": {"astra-cyber-35b-a3b:latest": 32768},
            "supports_tools": {"astra-cyber-35b-a3b:latest": True},
            "model_active_billions": {"astra-cyber-35b-a3b:latest": 3.0},
            "vram_bytes": 32 * (1024 ** 3),
            "kv_cache_type": "q8_0",
        },
    }
    pool = OllamaWorkerPool(PRIMARY, capability_prober=lambda origin: probes[origin])
    pool.refresh_capabilities(force=True)

    def fake_get(url, _headers, _timeout):
        if url.endswith("/v1/sonder/health"):
            return (200, json.dumps({
                "status": "ok",
                "api_version": 1,
                "version": "1.0",
                "models": [{"id": "sonder:latest"}],
            }).encode("utf-8"))
        raise AssertionError(f"unexpected GET {url}")

    gw = SonderInferenceGateway(
        SonderInferenceConfig(base_url="http://127.0.0.1:11437"),
        get_transport=fake_get,
        transport=lambda *_a, **_k: {},
        ollama_pool=pool,
    )
    gw.register_ollama_worker(NODE1, allow_remote=True, refresh=True)

    unified = gw.unified_worker_status()
    assert unified["worker_count"] == 2
    assert unified["healthy_worker_count"] == 2
    assert unified["total_vram_bytes"] == 48 * (1024 ** 3)
    assert unified["resident_models"] == ["astra-cyber-35b-a3b:latest", "sonder:latest"]
    assert unified["model_contexts"]["astra-cyber-35b-a3b:latest"] == 32768

    status = gw.provider_status()["sonder_inference"]
    assert status["ollama_pool"]["healthy_worker_count"] == 2
    assert len(status["pool_workers"]) == 2
