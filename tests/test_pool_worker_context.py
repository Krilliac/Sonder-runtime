"""Multi-worker pool capability discovery, scoring and context policy."""
from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import urllib.error

import pytest

from sonder_runtime.adapters.inference import ollama_pool
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

# Documentation-range addresses (RFC 5737); never a real host.
PRIMARY = "http://127.0.0.1:11434"
WORKER_A = "https://192.0.2.10:8443"
WORKER_B = "https://192.0.2.11:8443"
MOE = "moe-35b-a3b:latest"
GIB = 1024 ** 3


def _json_response(payload: dict) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


# ---------------------------------------------------------------------------
# _default_capability_prober: /api/ps, background-filled /api/show cache
# ---------------------------------------------------------------------------


def test_default_capability_prober_reads_ps_and_caches_show_by_digest():
    show_calls: list[tuple[str, str]] = []
    digest = "sha256:aaa111"

    def fake_open_url(request, **_kwargs):
        url = request.full_url
        if url.endswith("/api/version"):
            return _json_response({"version": "0.12.6"})
        if url.endswith("/api/tags"):
            return _json_response({
                "models": [
                    {"name": MOE, "digest": digest, "details": {"parameter_size": "35B"}},
                ]
            })
        if url.endswith("/api/ps"):
            return _json_response({
                "models": [
                    {
                        "name": MOE,
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
                    "moe.context_length": 262144,
                    "moe.expert_count": 256,
                    "moe.expert_used_count": 8,
                },
                "capabilities": ["completion", "tools", "thinking"],
            })
        raise AssertionError(f"unexpected URL {url}")

    probe = _default_capability_prober(allow_remote=True, open_url=fake_open_url)
    first = probe(WORKER_A)
    assert first["version"] == "0.12.6"
    assert first["models"] == (MOE,)
    assert first["loaded_models"] == (MOE,)
    # The "a3b" tag is the MoE active-parameter hint before /api/show is cached.
    assert first["model_active_billions"][MOE] == 3.0
    # Stock Ollama reports no host VRAM/RAM/KV-cache fields; none may be invented.
    assert "vram_bytes" not in first
    assert "ram_bytes" not in first
    assert "kv_cache_type" not in first
    # The probe never waits on /api/show; a background fill caches it.
    probe.wait_show_fill(10.0)
    assert len(show_calls) == 1
    first = probe(WORKER_A)
    assert first["supports_tools"][MOE] is True
    # Loaded model: the /api/ps runtime context (32768) wins over the trained
    # maximum (262144) that /api/show reports.
    assert first["model_contexts"][MOE] == 32768

    details = probe.show(WORKER_A, MOE, digest=digest)
    assert details["context_length"] == 262144
    assert details["supports_tools"] is True
    assert details["active_billions"] == 3.0
    assert len(show_calls) == 1

    second = probe(WORKER_A)
    assert second["model_contexts"][MOE] == 32768
    assert second["supports_tools"][MOE] is True
    probe.show(WORKER_A, MOE, digest=digest)
    assert len(show_calls) == 1

    # A new digest is a new model build: the cache misses and /api/show reruns.
    probe.show(WORKER_A, MOE, digest="sha256:bbb222")
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
    probe.wait_show_fill(10.0)


def test_default_capability_prober_bounds_show_fetches_per_probe():
    names = [f"m{i}:latest" for i in range(ollama_pool._SHOW_FETCHES_PER_PROBE + 4)]
    show_calls: list[str] = []

    def fake_open_url(request, **_kwargs):
        url = request.full_url
        if url.endswith("/api/version"):
            return _json_response({"version": "0.12.6"})
        if url.endswith("/api/tags"):
            return _json_response({"models": [{"name": n, "digest": "sha256:" + n} for n in names]})
        if url.endswith("/api/ps"):
            return _json_response({"models": []})
        if url.endswith("/api/show"):
            show_calls.append(json.loads(request.data.decode("utf-8"))["model"])
            return _json_response({"capabilities": ["completion", "tools"]})
        raise AssertionError(url)

    probe = _default_capability_prober(allow_remote=True, open_url=fake_open_url)
    probe(WORKER_A)
    probe.wait_show_fill(10.0)
    assert len(show_calls) == ollama_pool._SHOW_FETCHES_PER_PROBE
    first = probe(WORKER_A)
    assert len(first["supports_tools"]) == ollama_pool._SHOW_FETCHES_PER_PROBE
    probe.wait_show_fill(10.0)
    second = probe(WORKER_A)
    # Cached models are not refetched; the remainder is filled in.
    assert len(show_calls) == len(names)
    assert len(second["supports_tools"]) == len(names)


# ---------------------------------------------------------------------------
# register_worker, scoring and aggregate budgets
# ---------------------------------------------------------------------------


def _two_worker_reports() -> dict[str, dict]:
    return {
        PRIMARY: {
            "protocol": "ollama-http-v1",
            "version": "0.40.16",
            "models": ["qwen3.5:9b"],
            "loaded_models": ["qwen3.5:9b"],
            "model_contexts": {"qwen3.5:9b": 32768},
            "vram_bytes": 16 * GIB,
            "kv_cache_type": "f16",
            "supports_tools": {"qwen3.5:9b": True},
        },
        WORKER_A: {
            "protocol": "ollama-http-v1",
            "version": "0.40.16",
            "models": [MOE, "qwen3.5:9b"],
            "loaded_models": [MOE],
            "model_contexts": {MOE: 32768, "qwen3.5:9b": 65536},
            "vram_bytes": 32 * GIB,
            "kv_cache_type": "q8_0",
            "supports_tools": {MOE: True, "qwen3.5:9b": True},
            "model_active_billions": {MOE: 3.0},
        },
    }


def test_register_worker_connects_and_pools_a_remote_worker_dynamically():
    probes = _two_worker_reports()
    pool = OllamaWorkerPool(PRIMARY, capability_prober=lambda origin: probes[origin])
    pool.refresh_capabilities(force=True)
    assert pool.origins == (PRIMARY,)

    # The trust policy still applies: a remote worker must use HTTPS ...
    with pytest.raises(ValueError, match="https"):
        pool.register_worker("http://192.0.2.10:11434", allow_remote=True)
    # ... and needs remote consent.
    with pytest.raises(ValueError, match="SONDER_ALLOW_REMOTE_OLLAMA"):
        pool.register_worker(WORKER_A)
    assert pool.origins == (PRIMARY,)

    snap = pool.register_worker(WORKER_A, allow_remote=True, refresh=True)
    assert snap.origin == WORKER_A
    assert snap.healthy is True
    assert snap.kv_cache_type == "q8_0"
    assert snap.vram_bytes == 32 * GIB
    assert snap.model_contexts[MOE] == 32768
    assert pool.origins == (PRIMARY, WORKER_A)
    assert pool.enabled is True
    assert pool.has_configured_remote_workers is True

    # Re-registration refreshes in place without duplicating workers.
    pool.register_worker(WORKER_A + "/", allow_remote=True, refresh=False)
    assert pool.origins == (PRIMARY, WORKER_A)

    budget = pool.aggregate_context_budget(MOE)
    assert budget["eligible_workers"] == 1
    assert budget["max_context"] == 32768
    assert budget["recommended_worker_origin"] == WORKER_A
    assert budget["resident_origins"] == [WORKER_A]
    assert budget["supports_tools"] is True
    assert budget["active_billions"] == 3.0

    capacity = pool.pool_capacity_summary()
    assert capacity["worker_count"] == 2
    assert capacity["healthy_worker_count"] == 2
    assert capacity["total_vram_bytes"] == 48 * GIB
    assert capacity["model_contexts"]["qwen3.5:9b"] == 65536

    pool_wide = pool.aggregate_context_budget()
    assert pool_wide["max_context"] == 65536

    catalog_caps = pool_model_capabilities(pool)
    assert catalog_caps[MOE]["resident_origins"] == [WORKER_A]
    assert catalog_caps["qwen3.5:9b"]["origins"] == [PRIMARY, WORKER_A]
    assert catalog_caps["qwen3.5:9b"]["max_context"] == 65536


def test_register_worker_respects_max_workers():
    probes = _two_worker_reports()
    pool = OllamaWorkerPool(
        PRIMARY, capability_prober=lambda origin: probes[origin], max_workers=1,
    )
    with pytest.raises(ValueError, match="at most 1"):
        pool.register_worker(WORKER_A, allow_remote=True, refresh=False)
    assert pool.origins == (PRIMARY,)


def test_register_worker_refused_when_membership_owns_the_roster():
    probes = _two_worker_reports()
    pool = OllamaWorkerPool(PRIMARY, capability_prober=lambda origin: probes[origin])
    pool.configure_membership(
        cluster_id="cluster-a", issuer_id="issuer-a",
        clock=lambda: datetime.now(timezone.utc),
    )
    with pytest.raises(ValueError, match="membership"):
        pool.register_worker(WORKER_A, allow_remote=True, refresh=False)
    assert pool.origins == (PRIMARY,)


def test_register_loopback_worker_counts_toward_local_workers():
    second_local = "http://127.0.0.1:11435"
    probes = {
        PRIMARY: {"protocol": "ollama-http-v1", "version": "1", "models": ["a:1b"]},
        second_local: {"protocol": "ollama-http-v1", "version": "1", "models": ["a:1b"]},
    }
    pool = OllamaWorkerPool(
        PRIMARY, capability_prober=lambda origin: probes[origin], max_workers=4,
    )
    assert pool.membership_limit == 3
    pool.register_worker(second_local, refresh=False)
    # Local workers and remote members together stay within max_workers.
    assert pool.membership_limit == 2


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
        WORKER_A: {
            "protocol": "ollama-http-v1",
            "version": "0.40.16",
            "models": ["coder:35b"],
            "loaded_models": ["coder:35b"],
            "model_contexts": {"coder:35b": 32768},
            "latency_ms": 40.0,
        },
        WORKER_B: {
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
        (WORKER_A, WORKER_B),
        allow_remote=True,
        capability_prober=lambda origin: reports[origin],
    )
    pool.refresh_capabilities(force=True)

    chosen: list[str] = []
    # PRIMARY is fastest, but only WORKER_A has the model resident.
    pool.request(lambda origin: chosen.append(origin) or "ok", model="coder:35b")
    assert chosen[-1] == WORKER_A

    # Nothing resident and a 32768-token window wanted: WORKER_B (65536)
    # beats the faster PRIMARY (8192) and WORKER_A (16384).
    reports[WORKER_A]["loaded_models"] = []
    reports[WORKER_A]["model_contexts"]["coder:35b"] = 16384
    pool.refresh_capabilities(force=True)
    chosen.clear()
    pool.request(
        lambda origin: chosen.append(origin) or "ok",
        model="coder:35b",
        payload={"options": {"num_ctx": 32768}},
    )
    assert chosen[-1] == WORKER_B


def test_worker_capabilities_backward_compatible_construction():
    caps = WorkerCapabilities("ollama", "0.40.0", ("m:7b",), 100.0, 2)
    assert caps.model_contexts == {}
    assert caps.loaded_models == ()
    assert caps.vram_bytes is None
    assert caps.context_for("m:7b") is None
    assert caps.model_supports_tools("m:7b") is None
    assert caps.active_billions_for("m:7b") is None


# ---------------------------------------------------------------------------
# context_policy: MoE active parameters and worker VRAM / KV cache
# ---------------------------------------------------------------------------


def test_context_policy_uses_moe_active_billions_and_node_capabilities(monkeypatch):
    monkeypatch.delenv("SONDER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SONDER_SESSION_NUM_CTX", raising=False)
    monkeypatch.delenv("SONDER_KV_CACHE_TYPE", raising=False)
    monkeypatch.delenv("OLLAMA_KV_CACHE_TYPE", raising=False)

    # Dense 35B with a worker-reported q8_0 cache: the parameter band clamps.
    dense_q8 = context_policy.auto_context_plan(262144, "35B", node_kv_cache_type="q8_0")
    assert dense_q8["context"] == 16384
    assert dense_q8["kv_cache_source"] == "node-capability"
    assert "parameters>=24B" in dense_q8["clamps"]

    # Same total size, 3B active (MoE) on a 32 GiB q8_0 worker: full 32768.
    moe_plan = context_policy.auto_context_plan(
        262144,
        "35B",
        active_billions=3.0,
        node_vram_bytes=32 * GIB,
        node_kv_cache_type="q8_0",
    )
    assert moe_plan["context"] == 32768
    assert moe_plan["active_billions"] == 3.0
    assert moe_plan["node_vram_bytes"] == 32 * GIB
    assert "parameters>=24B" not in moe_plan["clamps"]

    # A small-VRAM worker caps the window whatever the model.
    small = context_policy.auto_context_plan(
        262144, "3B", node_vram_bytes=6 * GIB, node_kv_cache_type="q8_0",
    )
    assert small["context"] == 8192
    assert "node-vram-budget" in small["clamps"]

    # An operator pin still overrides automatic tuning.
    monkeypatch.setenv("SONDER_CONTEXT_SIZE", "12k")
    assert context_policy.auto_context(
        262144, "35B", active_billions=3.0, node_kv_cache_type="q8_0",
    ) == 12000


def test_context_policy_declared_kv_cache_beats_worker_report(monkeypatch):
    monkeypatch.delenv("SONDER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SONDER_SESSION_NUM_CTX", raising=False)
    monkeypatch.delenv("OLLAMA_KV_CACHE_TYPE", raising=False)
    monkeypatch.setenv("SONDER_KV_CACHE_TYPE", "f16")
    plan = context_policy.auto_context_plan(262144, "3B", node_kv_cache_type="q8_0")
    assert plan["kv_cache_type"] == "f16"
    assert plan["kv_cache_source"] == "declared"
    assert plan["base"] == context_policy.DEFAULT_CONTEXT_FP16_KV


def test_context_policy_without_worker_facts_is_unchanged(monkeypatch):
    monkeypatch.delenv("SONDER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SONDER_SESSION_NUM_CTX", raising=False)
    monkeypatch.delenv("SONDER_KV_CACHE_TYPE", raising=False)
    monkeypatch.delenv("OLLAMA_KV_CACHE_TYPE", raising=False)
    plan = context_policy.auto_context_plan(262144, "35B")
    assert plan["kv_cache_source"] == "default"
    assert plan["active_billions"] is None
    assert plan["node_vram_bytes"] is None
    assert plan["context"] == context_policy.DEFAULT_CONTEXT_FP16_KV


# ---------------------------------------------------------------------------
# ResidencyFeedback per-origin keying; remote worker thread options
# ---------------------------------------------------------------------------


def test_residency_feedback_isolates_ceilings_by_origin():
    ps_by_origin = {
        PRIMARY: {
            "models": [{"name": "qwen3.6:35b", "size": 20_000_000_000, "size_vram": 15_000_000_000}],
        },
        WORKER_A: {
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
        origin=WORKER_A,
    )

    primary_verdict = feedback.refresh("qwen3.6:35b", geometry=None, kv_type="f16", origin=PRIMARY)
    worker_verdict = feedback.refresh("qwen3.6:35b", geometry=None, kv_type="q8_0", origin=WORKER_A)
    assert primary_verdict is not None and primary_verdict.placement == "gpu+ram-hybrid"
    assert worker_verdict is not None and worker_verdict.placement == "gpu-resident"
    assert feedback.ceiling("qwen3.6:35b", origin=PRIMARY) == 8192
    assert feedback.ceiling("qwen3.6:35b", origin=WORKER_A) is None
    # Without an origin the feedback's own origin is used.
    assert feedback.ceiling("qwen3.6:35b") == 8192


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


def test_environment_options_local_auto_thread_still_sends_none():
    # "auto" on the coordinator means "let Ollama decide", as before.
    assert "num_thread" not in local_model_options(
        0.2, 256, 8192, environ={"SONDER_NUM_THREAD": "auto"},
    )
    assert "num_thread" not in local_model_options(
        0.2, 256, 8192, environ={"SONDER_NUM_THREAD": "auto"}, remote=True,
    )


# ---------------------------------------------------------------------------
# SonderInferenceGateway unified pool discovery and health
# ---------------------------------------------------------------------------


def _health_get(url, _headers, _timeout):
    if url.endswith("/v1/sonder/health"):
        return (200, json.dumps({
            "status": "ok",
            "api_version": 1,
            "version": "1.0",
            "models": [{"id": "sonder:latest"}],
        }).encode("utf-8"))
    raise AssertionError(f"unexpected GET {url}")


def test_sonder_inference_gateway_unifies_ollama_pool_snapshots():
    probes = {
        PRIMARY: {
            "protocol": "ollama-http-v1",
            "version": "0.40.16",
            "models": ["sonder:latest"],
            "loaded_models": ["sonder:latest"],
            "model_contexts": {"sonder:latest": 32768},
            "supports_tools": {"sonder:latest": True},
            "vram_bytes": 16 * GIB,
        },
        WORKER_A: {
            "protocol": "ollama-http-v1",
            "version": "0.40.16",
            "models": [MOE],
            "loaded_models": [MOE],
            "model_contexts": {MOE: 32768},
            "supports_tools": {MOE: True},
            "model_active_billions": {MOE: 3.0},
            "vram_bytes": 32 * GIB,
            "kv_cache_type": "q8_0",
        },
    }
    pool = OllamaWorkerPool(PRIMARY, capability_prober=lambda origin: probes[origin])
    pool.refresh_capabilities(force=True)

    gw = SonderInferenceGateway(
        SonderInferenceConfig(base_url="http://127.0.0.1:11437"),
        get_transport=_health_get,
        transport=lambda *_a, **_k: {},
        ollama_pool=pool,
    )
    gw.register_ollama_worker(WORKER_A, allow_remote=True, refresh=True)

    unified = gw.unified_worker_status()
    assert unified["worker_count"] == 2
    assert unified["healthy_worker_count"] == 2
    assert unified["total_vram_bytes"] == 48 * GIB
    assert unified["resident_models"] == [MOE, "sonder:latest"]
    assert unified["model_contexts"][MOE] == 32768

    status = gw.provider_status()["sonder_inference"]
    assert status["ollama_pool"]["healthy_worker_count"] == 2
    assert len(status["pool_workers"]) == 2


def test_gateway_without_pool_keeps_status_shape_and_refuses_registration():
    gw = SonderInferenceGateway(
        SonderInferenceConfig(base_url="http://127.0.0.1:11437"),
        get_transport=_health_get,
        transport=lambda *_a, **_k: {},
    )
    status = gw.provider_status()["sonder_inference"]
    assert "ollama_pool" not in status
    assert "pool_workers" not in status
    with pytest.raises(Exception, match="no OllamaWorkerPool"):
        gw.register_ollama_worker(WORKER_A, allow_remote=True)


def test_gateway_provider_status_survives_a_failing_pool():
    class BrokenPool:
        def pool_capacity_summary(self):
            raise RuntimeError("pool exploded")

        def snapshots(self):
            raise RuntimeError("pool exploded")

    gw = SonderInferenceGateway(
        SonderInferenceConfig(base_url="http://127.0.0.1:11437"),
        get_transport=_health_get,
        transport=lambda *_a, **_k: {},
        ollama_pool=BrokenPool(),
    )
    # provider_status never raises: the gateway entry is still reported and
    # the pool fields are left out.
    status = gw.provider_status()["sonder_inference"]
    assert status["base_url"] == "http://127.0.0.1:11437"
    assert "ollama_pool" not in status
    assert "pool_workers" not in status
