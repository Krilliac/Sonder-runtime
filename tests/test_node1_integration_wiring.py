"""Integration tests for Node1 pool/context/ensemble wiring and Mode.BYPASS restricted lanes."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import permission_modes as pm
import server
from sonder_runtime.adapters import restricted_tool_lane as rtl
from sonder_runtime.adapters.inference.ollama_pool import OllamaWorkerPool
from sonder_runtime.adapters.inference.pool_model_catalog import PoolCatalogCache
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceConfig,
    SonderInferenceGateway,
)
from sonder_runtime.adapters.security import role_tool_policy
from sonder_runtime.bootstrap import pool_wiring

pytestmark = pytest.mark.unit

LOCAL = "http://127.0.0.1:11434"
NODE1 = "https://10.77.0.2:8443"


@pytest.fixture(autouse=True)
def _restore_permission_state():
    saved_state = dict(pm._STATE)
    saved_scopes = dict(pm._SCOPED_MODES)
    saved_loaded = pm._LOADED
    yield
    with pm._LOCK:
        pm._STATE.clear()
        pm._STATE.update(saved_state)
        pm._SCOPED_MODES.clear()
        pm._SCOPED_MODES.update(saved_scopes)
        pm._LOADED = saved_loaded


def test_attach_and_register_worker_and_unified_admin_status(monkeypatch):
    gateway = SonderInferenceGateway(SonderInferenceConfig(base_url="http://127.0.0.1:11437"))
    probes = {
        LOCAL: {"protocol": "ollama-http-v1", "version": "0.40.16", "models": ["sonder:latest"]},
        NODE1: {
            "protocol": "ollama-http-v1",
            "version": "0.40.16",
            "models": ["qwen3.6:35b", "astra-cyber-35b-a3b:latest"],
            "loaded_models": ["qwen3.6:35b"],
            "model_contexts": {"qwen3.6:35b": 65536, "astra-cyber-35b-a3b:latest": 65536},
            "model_active_billions": {"qwen3.6:35b": 3.0, "astra-cyber-35b-a3b:latest": 3.0},
            "vram_bytes": 32 * (1024 ** 3),
            "kv_cache_type": "q8_0",
        },
    }
    pool = OllamaWorkerPool(LOCAL, (), allow_remote=True, capability_prober=lambda o: probes[o])
    pool.refresh_capabilities()

    monkeypatch.setattr(server, "BASE", LOCAL)
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(server, "_APP_GRAPH", SimpleNamespace(model_gateway=gateway))
    monkeypatch.setattr(server, "_POOL_PIN_CATALOG", PoolCatalogCache())

    snap = server.register_pool_worker(NODE1, allow_remote=True, refresh=True)
    assert NODE1 in snap.origin
    assert "qwen3.6:35b" in snap.models

    status = server._ollama_pool_admin_status_data(principal="local-open", refresh=False)
    assert "capacity_summary" in status and status["capacity_summary"]["healthy_worker_count"] == 2
    assert "unified" in status
    assert "qwen3.6:35b" in status["unified"]["available_models"]


def test_auto_model_context_and_remote_num_thread_omission(monkeypatch):
    monkeypatch.delenv("SONDER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SONDER_SESSION_NUM_CTX", raising=False)
    monkeypatch.delenv("SONDER_KV_CACHE_TYPE", raising=False)
    monkeypatch.delenv("SONDER_NUM_THREAD", raising=False)
    server._MODEL_CONTEXT_CACHE.clear()
    server._MODEL_GEOMETRY_CACHE.clear()
    server._MODEL_CONTEXT_MEMORY.clear()

    probes = {
        LOCAL: {"protocol": "ollama-http-v1", "version": "0.40.16", "models": ["sonder:latest"]},
        NODE1: {
            "protocol": "ollama-http-v1",
            "version": "0.40.16",
            "models": ["qwen3.6:35b"],
            "loaded_models": ["qwen3.6:35b"],
            "model_contexts": {"qwen3.6:35b": 262144},
            "model_active_billions": {"qwen3.6:35b": 3.0},
            "vram_bytes": 32 * (1024 ** 3),
            "kv_cache_type": "q8_0",
        },
    }
    pool = OllamaWorkerPool(LOCAL, (NODE1,), allow_remote=True, capability_prober=lambda o: probes[o])
    pool.refresh_capabilities()

    monkeypatch.setattr(server, "BASE", LOCAL)
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(server, "_RESIDENCY_FEEDBACK", None)
    monkeypatch.setattr(server, "_model_context_metadata", lambda _m: (262144, "35B"))
    monkeypatch.setattr(server, "_get", lambda _p: {"models": []})

    window = server._auto_model_context("qwen3.6:35b")
    assert window == 32768  # q8_0 + 32 GiB Node1 + 3B active MoE avoids dense-35B 16K/8K clamp

    payload = {"model": "qwen3.6:35b", "options": {"num_ctx": window, "num_thread": 24}}
    cleaned, stripped = pool_wiring.strip_default_num_thread_for_remote(
        payload,
        origin=NODE1,
        is_loopback_fn=server.ollama_endpoint.is_loopback,
        env_int_fn=server._env_int_option,
    )
    assert stripped is True and "num_thread" not in cleaned["options"]


def _reroute(monkeypatch, *, loopback_endpoint):
    from sonder_runtime.adapters import legacy_chat_bridge
    from sonder_runtime.application.chat import provider_bridge

    rung = SimpleNamespace(provider="sonder_inference", tier="general")
    monkeypatch.setattr(
        legacy_chat_bridge,
        "provider_bindings",
        lambda _g: SimpleNamespace(fallbacks={"sonder_inference": "ollama"}),
    )
    monkeypatch.setattr(provider_bridge, "record_degradation", lambda _s: None)
    return legacy_chat_bridge.ollama_only_reroute(
        SimpleNamespace(),
        {"model": "qwen3.6:35b", "format": {"type": "object"}, "options": {}},
        rung,
        model="qwen3.6:35b",
        graph=SimpleNamespace(),
        loopback_endpoint=loopback_endpoint,
        context_probe=lambda: 32768,
    )


def test_ollama_only_reroute_on_loopback_may_be_placed_on_configured_pool_workers(monkeypatch):
    from sonder_runtime.adapters import legacy_chat_bridge

    rerouted = _reroute(monkeypatch, loopback_endpoint=True)
    assert rerouted is not None and rerouted["options"]["num_ctx"] == 32768
    pool = OllamaWorkerPool(LOCAL, (NODE1,), allow_remote=True, capability_prober=lambda o: {})
    assert pool.enabled and pool.has_remote_workers
    assert legacy_chat_bridge.reroute_may_use_pool(pool, base_loopback=True, local_only=False) is True
    assert legacy_chat_bridge.reroute_may_use_pool(pool, base_loopback=True, local_only=True) is False
    solo = OllamaWorkerPool(LOCAL, (), allow_remote=True, capability_prober=lambda o: {})
    assert legacy_chat_bridge.reroute_may_use_pool(solo, base_loopback=True, local_only=False) is False


def test_ollama_only_reroute_refuses_a_remote_primary_even_with_a_pool(monkeypatch):
    """A remote primary plus one loopback worker made the primary eligible; it must not."""
    from sonder_runtime.adapters import legacy_chat_bridge

    remote_primary = "https://ollama.example.net:11434"
    pool = OllamaWorkerPool(remote_primary, ("http://127.0.0.1:11435",), allow_remote=True,
                            capability_prober=lambda o: {})
    assert pool.enabled and pool.has_remote_workers  # the primary itself is the remote member
    assert legacy_chat_bridge.reroute_may_use_pool(pool, base_loopback=False, local_only=False) is False
    with pytest.raises(server.ModelCallError) as caught:
        _reroute(monkeypatch, loopback_endpoint=False)
    assert "remote" in caught.value.detail


def test_ensemble_answer_runs_distinct_nodes_concurrently_and_skips_unneeded_unload(monkeypatch):
    probes = {
        LOCAL: {"protocol": "ollama-http-v1", "version": "0.40.16", "models": ["local:7b"]},
        NODE1: {"protocol": "ollama-http-v1", "version": "0.40.16", "models": ["node1:35b"]},
    }
    pool = OllamaWorkerPool(LOCAL, (NODE1,), allow_remote=True, capability_prober=lambda o: probes[o])
    pool.refresh_capabilities()

    monkeypatch.setattr(server, "BASE", LOCAL)
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(
        server,
        "_serve_target",
        lambda tier, strict, **_kw: ({"a": "local:7b", "b": "node1:35b"}[tier], False, False, tier),
    )

    entered: list[str] = []
    barrier = threading.Barrier(2, timeout=2.0)

    def make(tier, model, *_a, **_kw):
        def gen(prompt, history=None):
            if "COMPOUNDED ANSWER:" in prompt:
                return "merged across nodes"
            entered.append(model)
            barrier.wait()  # Proves both distinct-node members execute concurrently.
            return f"ans-{model}"

        gen.last_response_meta = {"served_model": f"{model}-served"}
        return gen

    unloaded: list[tuple[str | None, str | None]] = []
    monkeypatch.setattr(server, "_make_tier_generate", make)
    monkeypatch.setattr(server, "_auto_model_context", lambda _m, origin=None: 8192)
    monkeypatch.setattr(
        server,
        "_post",
        lambda path, payload, **k: unloaded.append((server._PINNED_OLLAMA_ORIGIN.get(), payload.get("model"))) or {},
    )

    out = server.ensemble_answer("q", tiers="a,b")
    assert set(entered) == {"local:7b", "node1:35b"}
    assert "local:7b-served" in out and "node1:35b-served" in out
    assert unloaded == []  # 1 model per node -> no unnecessary VRAM eviction


def test_bypass_mode_allows_restricted_tool_lane_and_dangerous_tools_while_keeping_role_bounds(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_PERMISSION_STATE", str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    denied_rules = {
        # The unmodified shipped default: a deny, so it holds in bypass too.
        "file_delete": {"pattern": "file_delete", "action": "deny", "note": "destructive by default"},
        "sqlite_mutate": {"pattern": "sqlite_mutate", "action": "ask", "note": "db mutate"},
        "admin_private_chain_of_thought": {
            "pattern": "admin_private_chain_of_thought",
            "action": "deny",
            "note": "always deny",
        },
    }
    monkeypatch.setattr(pm, "_rule_lookup", lambda n: denied_rules.get(n))

    with pm.attended_mode_change(), pm.attended_bypass_confirm():
        pm.set_mode(pm.BYPASS)

    assert rtl.admin_opt_in_active() is True
    entry = rtl.entry("agent", str(tmp_path))
    run, refusal = rtl.admit(
        entry,
        "astra-cyber-35b-a3b",
        "tool-lane-denied",
        None,
        unsafe=False,
        prepared=False,
        resolve=lambda: ("astra-cyber-35b-a3b", False, False, "model:astra-cyber-35b-a3b"),
        provider_for=lambda _l: None,
    )
    assert refusal == "" and run is not None and run.admin_open is True
    policy = run.tool_policy(None, lambda _t, a, _r: a)
    assert policy("file_delete", {"path": str(tmp_path / "x.txt")}) == ""
    assert run.filter_observation("text_search", {}, "raw lines") == "raw lines"

    # A sqlite_mutate ask is bypassed for local-open/admin; bypass skips ``ask`` only, so
    # every deny rule, the shipped file_delete one included, still holds
    # (tests/test_perm_owner_binding.py).
    assert "rule denies" in server._agent_permission_gate_error("file_delete", {"path": "x.txt"}, rule_lookup=denied_rules.get)
    assert server._agent_permission_gate_error("sqlite_mutate", {"path": "a.db"}, rule_lookup=denied_rules.get) == ""
    # Protected boundaries remain enforced even in BYPASS.
    assert "system operation" in server._agent_dispatch("permission_mode", {"mode": "auto"})
    assert server._agent_permission_gate_error("admin_private_chain_of_thought", {}, rule_lookup=denied_rules.get) != ""
    with role_tool_policy.caller_role_scope("user"):
        assert rtl.admin_opt_in_active() is False
        assert server._agent_permission_gate_error("file_delete", {"path": "x.txt"}, rule_lookup=denied_rules.get) != ""


_INFERENCE_ENV = ("SONDER_INFERENCE_BASE_URL", "SONDER_INFERENCE_READY_FILE",
                  "SONDER_INFERENCE_PRIVATE_WORKERS", "SONDER_ALLOW_REMOTE_INFERENCE",
                  "SONDER_INFERENCE_API_KEY")


def _admit_inference_model(monkeypatch, **env):
    from sonder_runtime.adapters import tool_lane_guard

    for name in _INFERENCE_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    model = "astra-cyber-35b-a3b:latest"
    with rtl.admin_opt_in(True):
        return rtl.admit(
            rtl.entry("agent", ""), model, tool_lane_guard.DENIED_LABEL, server._RUNTIME_POLICY,
            unsafe=False, prepared=False,
            resolve=lambda: (model, False, False, "model:%s" % model),
            provider_for=lambda _label: "sonder_inference",
        )


def test_restricted_lane_counts_sonder_inference_as_local_only_on_loopback(monkeypatch):
    run, refusal = _admit_inference_model(monkeypatch)  # default endpoint: 127.0.0.1
    assert refusal == "" and run is not None and run.admin_open is True
    run, refusal = _admit_inference_model(
        monkeypatch, SONDER_INFERENCE_BASE_URL="http://localhost:11437")
    assert run is not None


def test_restricted_lane_refuses_sonder_inference_on_a_remote_endpoint(monkeypatch):
    """A deny-listed model's unfiltered BYPASS observations must not go to a remote endpoint."""
    run, refusal = _admit_inference_model(
        monkeypatch, SONDER_INFERENCE_BASE_URL="https://inference.example.net:8443",
        SONDER_ALLOW_REMOTE_INFERENCE="1", SONDER_INFERENCE_API_KEY="k")
    assert run is None and refusal == ""


def test_restricted_lane_refuses_sonder_inference_when_its_endpoint_cannot_be_read(monkeypatch):
    run, refusal = _admit_inference_model(monkeypatch, SONDER_INFERENCE_BASE_URL="ftp://nowhere")
    assert run is None and refusal == ""
