"""RET-05 Subsystem 4 & 5 tests: Ollama Cloud via SI RET-02, consent fence, and SI residency/fanout."""
from __future__ import annotations

import inspect
import json

import pytest

from sonder_runtime.adapters.inference import gpu_residency
from sonder_runtime.adapters.inference.ollama_cloud_metadata import (
    OllamaCloudMetadata,
    OllamaPullFailed,
)
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceGateway,
    correlation_headers,
)
from sonder_runtime.adapters.ollama_lifecycle import (
    cleanup_orphaned_discovery_probes,
    resident_models,
)
from sonder_runtime.adapters.provider_bindings import ProviderBindings
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.bootstrap import pool_wiring
from sonder_runtime.domain.cloud_model_refresh import ProbeStatus, is_pulled_cloud_row
from sonder_runtime.domain.common.errors import Forbidden
from sonder_runtime.domain.fanout_residency import dispatch_residency_reason


def test_correlation_headers_send_local_only_fence_when_cloud_disallowed():
    local_ctx = local_owner_context(correlation_id="c-local", cloud_allowed=False)
    headers = correlation_headers(local_ctx)
    assert headers.get("X-Sonder-Local-Only") == "1"

    cloud_ctx = local_owner_context(correlation_id="c-cloud", cloud_allowed=True)
    cloud_headers = correlation_headers(cloud_ctx)
    assert "X-Sonder-Local-Only" not in cloud_headers


def test_loopback_si_ollama_cloud_refuses_without_cloud_consent_before_send():
    posts = []

    def fake_get(url, headers, timeout):
        if url.endswith("/v1/sonder/health"):
            return 200, json.dumps({
                "api_version": 1,
                "status": "ready",
                "backend": "ollama_cloud",
                "models": [{"id": "gpt-oss:120b", "state": "unloaded", "remote": True}],
            }).encode("utf-8")
        return 404, b"{}"

    def fake_post(url, body, headers, timeout):
        posts.append((url, body, headers))
        return {
            "id": "chatcmpl-1",
            "model": "gpt-oss:120b",
            "choices": [{"message": {"role": "assistant", "content": "cloud ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }

    gw = SonderInferenceGateway(
        env={"SONDER_INFERENCE_BASE_URL": "http://127.0.0.1:11437", "SONDER_INFERENCE_MODEL": "gpt-oss:120b"},
        get_transport=fake_get,
        transport=fake_post,
    )
    local_ctx = local_owner_context(correlation_id="no-cloud", cloud_allowed=False)
    with pytest.raises(Forbidden, match="cloud"):
        gw.generate(ModelRequest(prompt="secret", tier="code"), local_ctx)
    assert posts == []

    # Even with cloud_allowed=True in context, missing SONDER_ALLOW_CLOUD=1 must refuse.
    ctx_cloud = local_owner_context(correlation_id="yes-ctx-no-env", cloud_allowed=True)
    with pytest.raises(Forbidden, match="SONDER_ALLOW_CLOUD"):
        gw.generate(ModelRequest(prompt="hello", tier="code"), ctx_cloud)
    assert posts == []

    # With both SONDER_ALLOW_CLOUD=1 and cloud_allowed=True, request reaches SI ollama_cloud.
    gw_allowed = SonderInferenceGateway(
        env={
            "SONDER_INFERENCE_BASE_URL": "http://127.0.0.1:11437",
            "SONDER_INFERENCE_MODEL": "gpt-oss:120b",
            "SONDER_ALLOW_CLOUD": "1",
        },
        get_transport=fake_get,
        transport=fake_post,
    )
    resp = gw_allowed.generate(ModelRequest(prompt="hello", tier="code"), ctx_cloud)
    assert resp.text == "cloud ok"
    assert len(posts) == 1
    assert "X-Sonder-Local-Only" not in posts[0][2]


def test_ollama_cloud_metadata_uses_si_models_and_retires_stub_pulls():
    seen_urls = []

    class _FakeResp:
        def __init__(self, body: dict, status: int = 200):
            self._raw = json.dumps(body).encode("utf-8")
            self.status = status

        def read(self, n=-1):
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout=5.0, allow_remote=None):
        seen_urls.append(req.full_url)
        if req.full_url.endswith("/v1/models"):
            return _FakeResp({
                "object": "list",
                "data": [
                    {
                        "id": "gpt-oss:120b",
                        "object": "model",
                        "owned_by": "sonder-inference",
                        "sonder": {"backend": "ollama_cloud", "default": False, "synthetic": False},
                    },
                    {
                        "id": "qwen3.6:35b",
                        "object": "model",
                        "owned_by": "sonder-inference",
                        "sonder": {
                            "backend": "llamacpp",
                            "default": True,
                            "synthetic": False,
                            "runtime": {"context_tokens": 32768},
                        },
                    },
                ],
                "sonder": {"api_version": 1},
            })
        raise AssertionError(f"unexpected URL: {req.full_url}")

    client = OllamaCloudMetadata("http://127.0.0.1:11437", opener=fake_open)
    rows = client.tags()
    assert len(rows) == 1
    assert rows[0]["name"] == "gpt-oss:120b"
    assert rows[0]["remote_host"] is True
    assert all("/v1/models" in u for u in seen_urls)
    assert not any("/api/" in u for u in seen_urls)

    live = client.show("gpt-oss:120b")
    assert live.status is ProbeStatus.LIVE
    missing = client.show("retired-model:cloud")
    assert missing.status is ProbeStatus.NOT_FOUND

    with pytest.raises((OllamaPullFailed, ValueError)):
        client.pull_stub("qwen3-coder:480b-cloud")
    assert not any("/api/pull" in u for u in seen_urls)

    assert is_pulled_cloud_row({
        "id": "gpt-oss:120b",
        "object": "model",
        "owned_by": "sonder-inference",
        "sonder": {"backend": "ollama_cloud", "default": False, "synthetic": False},
    }) is True
    assert is_pulled_cloud_row({
        "id": "qwen3.6:35b",
        "object": "model",
        "owned_by": "sonder-inference",
        "sonder": {"backend": "llamacpp", "default": True, "synthetic": False},
    }) is False


def test_fanout_and_lifecycle_residency_use_si_health_shape():
    si_health = {
        "api_version": 1,
        "state": "ready",
        "models": [
            {"id": "qwen3.6:35b", "state": "resident"},
            {"id": "gemma3:27b", "state": "unloaded"},
        ],
    }
    assert dispatch_residency_reason(
        {"selection_profile": "loaded-local-chat"}, "qwen3.6:35b", fetch_resident=lambda: si_health,
    ) == ""
    assert "no longer resident" in dispatch_residency_reason(
        {"selection_profile": "loaded-local-chat"}, "gemma3:27b", fetch_resident=lambda: si_health,
    )
    assert "could not verify" in dispatch_residency_reason(
        {"selection_profile": "loaded-local-chat"}, "qwen3.6:35b", fetch_resident=lambda: {"bad": 1},
    )
    assert resident_models(si_health) == {"qwen3.6:35b"}
    assert cleanup_orphaned_discovery_probes()["terminated"] == []


def test_gpu_residency_and_pool_wiring_have_no_local_ollama_contention_or_api_calls():
    bindings = ProviderBindings.uniform("sonder_inference")
    assert gpu_residency.gpu_sharing_findings({}, bindings, inference_local=True) == []
    assert gpu_residency.keep_alive_for("qwen3.6:35b", "2m", primary=lambda: "qwen3.6:35b") == "2m"
    src = inspect.getsource(pool_wiring)
    assert "/api/ps" not in src
    assert "/api/generate" not in src


def test_si_v1_models_envelope_parsed_by_catalog_inventory_and_pool():
    """BLOCKING (2) + Round 3: real SI /v1/models and /v1/sonder/health envelopes work everywhere."""
    from sonder_runtime.domain.model_catalog import catalog_names, catalog_records, installed_records
    from sonder_runtime.adapters.model_inventory import inventory_rows
    from sonder_runtime.adapters.inference import ollama_pool
    from sonder_runtime.adapters.inference.model_context_probe import (
        extract_chat_fitness_payload,
        parse_model_metadata,
    )
    from sonder_runtime.domain.chat_model_fitness import unfit_reason

    si_models = {
        "object": "list",
        "data": [
            {
                "id": "qwen3.6:35b",
                "object": "model",
                "owned_by": "sonder-inference",
                "sonder": {
                    "backend": "llamacpp",
                    "default": True,
                    "synthetic": False,
                    "runtime": {
                        "gpu_memory": {"status": "ok", "spilled": False},
                        "context": {
                            "policy": "fit",
                            "configured_ctx": 65536,
                            "fitted_ctx": 32768,
                            "fit_attempts": 1,
                            "outcome": "fitted",
                        },
                        "warnings": [],
                    },
                    "profile": {
                        "name": "qwen3.6:35b",
                        "backend": "llamacpp",
                        "context_length": 32768,
                        "configured_context_length": 65536,
                        "parallel": 1,
                        "cache_type_k": "q8_0",
                        "cache_type_v": "q8_0",
                        "flash_attn": "on",
                        "capabilities": ["speculative"],
                        "upstream_capabilities": ["tools", "speculative"],
                        "speculative_types": ["ngram-mod"],
                    },
                },
            },
            {
                "id": "nomic-embed-text:latest",
                "object": "model",
                "owned_by": "sonder-inference",
                "capabilities": ["embedding"],
                "sonder": {
                    "backend": "llamacpp",
                    "default": False,
                    "synthetic": False,
                    "profile": {
                        "name": "nomic-embed-text:latest",
                        "backend": "llamacpp",
                        "context_length": 2048,
                        "capabilities": [],
                        "upstream_capabilities": [],
                    },
                },
            },
            {
                "id": "tiny-chat:latest",
                "object": "model",
                "owned_by": "sonder-inference",
                "sonder": {
                    "backend": "llamacpp",
                    "default": False,
                    "synthetic": False,
                    "runtime": {
                        "context": {
                            "policy": "fit",
                            "configured_ctx": 512,
                            "fitted_ctx": 512,
                            "fit_attempts": 1,
                            "outcome": "fitted",
                        },
                    },
                    "profile": {
                        "name": "tiny-chat:latest",
                        "backend": "llamacpp",
                        "context_length": 512,
                        "capabilities": [],
                        "upstream_capabilities": [],
                    },
                },
            },
        ],
        "sonder": {"api_version": 1},
    }
    assert catalog_names(si_models) == ["nomic-embed-text:latest", "qwen3.6:35b", "tiny-chat:latest"]
    recs = catalog_records(si_models)
    assert [name for name, _ in recs] == ["nomic-embed-text:latest", "qwen3.6:35b", "tiny-chat:latest"]
    assert recs[1][1]["name"] == "qwen3.6:35b"
    inst = installed_records(si_models)
    assert [name for name, _ in inst] == ["qwen3.6:35b", "nomic-embed-text:latest", "tiny-chat:latest"]

    inv = inventory_rows(si_models, "/v1/models")
    assert len(inv) == 3
    assert inv[0]["name"] == "qwen3.6:35b"

    assert ollama_pool._model_names(si_models) == (
        "nomic-embed-text:latest", "qwen3.6:35b", "tiny-chat:latest",
    )

    # parse_model_metadata reads real SI runtime.context.fitted_ctx and profile.context_length.
    ctx, _param, _geom, declared = parse_model_metadata(si_models, "qwen3.6:35b")
    assert ctx == 32768
    assert declared == 32768

    # Chat-fitness gate unwraps the matching data[] row and rejects embedding-only / tiny-context models.
    assert unfit_reason(extract_chat_fitness_payload(si_models, "qwen3.6:35b")) == ""
    assert "embedding" in unfit_reason(extract_chat_fitness_payload(si_models, "nomic-embed-text:latest")).lower()
    assert "minimum" in unfit_reason(extract_chat_fitness_payload(si_models, "tiny-chat:latest")).lower()

    # Prober reads /v1/sonder/health residency state/resident flags (never counting lazy/evicted rows as loaded)
    # and extracts per-model context from real /v1/models runtime.context.fitted_ctx / profile.context_length.
    si_health = {
        "status": "ready",
        "api_version": 1,
        "version": "1.0.0",
        "models": [
            {"id": "qwen3.6:35b", "backend": "llamacpp", "default": True, "state": "resident", "resident": True},
            {"id": "nomic-embed-text:latest", "backend": "llamacpp", "default": False, "state": "lazy", "resident": False},
            {"id": "tiny-chat:latest", "backend": "llamacpp", "default": False, "state": "evicted", "resident": False},
        ],
    }

    class _Resp:
        def __init__(self, body):
            self._body = json.dumps(body).encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return self._body

    def fake_open(req, timeout=2.0, allow_remote=False):
        if req.full_url.endswith("/v1/sonder/health"):
            return _Resp(si_health)
        if req.full_url.endswith("/v1/models"):
            return _Resp(si_models)
        raise AssertionError(f"unexpected URL: {req.full_url}")

    prober = ollama_pool._default_capability_prober(allow_remote=False, open_url=fake_open)
    result = prober("http://127.0.0.1:11437")
    assert "qwen3.6:35b" in result["models"]
    assert result["loaded_models"] == ("qwen3.6:35b",)
    assert "nomic-embed-text:latest" not in result["loaded_models"]
    assert "tiny-chat:latest" not in result["loaded_models"]
    assert result["model_contexts"]["qwen3.6:35b"] == 32768
    assert result["model_contexts"]["nomic-embed-text:latest"] == 2048
    assert result["model_contexts"]["tiny-chat:latest"] == 512


def test_server_chat_model_unfit_reason_rejects_embedding_only_si_model(monkeypatch):
    """Round 4 (R1): _CHAT_FITNESS keeps SI ['speculative'] profiles fit and rejects tiny-context/embedding-only models."""
    import server as srv

    si_models = {
        "object": "list",
        "data": [
            {
                "id": "nomic-embed-text:latest",
                "object": "model",
                "owned_by": "sonder-inference",
                "capabilities": ["embedding"],
                "sonder": {
                    "backend": "llamacpp",
                    "profile": {
                        "name": "nomic-embed-text:latest",
                        "backend": "llamacpp",
                        "context_length": 2048,
                        "capabilities": [],
                        "upstream_capabilities": [],
                    },
                },
            },
            {
                "id": "qwen3.8:27b",
                "object": "model",
                "owned_by": "sonder-inference",
                "sonder": {
                    "backend": "llamacpp",
                    "runtime": {"context": {"fitted_ctx": 32768}},
                    "profile": {
                        "name": "qwen3.8:27b",
                        "backend": "llamacpp",
                        "context_length": 32768,
                        "capabilities": ["speculative"],
                        "upstream_capabilities": ["tools", "speculative"],
                        "speculative_types": ["ngram-mod"],
                    },
                },
            },
            {
                "id": "tiny-model:latest",
                "object": "model",
                "owned_by": "sonder-inference",
                "sonder": {
                    "backend": "llamacpp",
                    "runtime": {"context": {"fitted_ctx": 512}},
                    "profile": {
                        "name": "tiny-model:latest",
                        "backend": "llamacpp",
                        "context_length": 512,
                        "capabilities": [],
                        "upstream_capabilities": [],
                    },
                },
            },
        ],
    }
    monkeypatch.setattr(srv, "_get", lambda path, **_kw: si_models if path == "/v1/models" else {})
    srv._CHAT_FITNESS._cache.clear()
    try:
        assert srv.chat_model_unfit_reason("qwen3.8:27b") == ""
        reason = srv.chat_model_unfit_reason("nomic-embed-text:latest")
        assert reason and "embedding" in reason.lower()
        tiny_reason = srv.chat_model_unfit_reason("tiny-model:latest")
        assert tiny_reason and "minimum" in tiny_reason.lower()
    finally:
        srv._CHAT_FITNESS._cache.clear()


def test_cloud_metadata_show_reads_real_si_fitted_ctx_and_honors_api_key_file(tmp_path, monkeypatch):
    """Round 3: OllamaCloudMetadata reads sonder.runtime.context.fitted_ctx and api_key_file."""
    key_file = tmp_path / "si.key"
    key_file.write_text("file-bearer-token-99\n", encoding="utf-8")
    monkeypatch.delenv("SONDER_INFERENCE_API_KEY", raising=False)
    monkeypatch.setenv("SONDER_INFERENCE_API_KEY_FILE", str(key_file))

    seen_headers = []
    si_models = {
        "object": "list",
        "data": [
            {
                "id": "kimi-k2.5:cloud",
                "object": "model",
                "owned_by": "sonder-inference",
                "sonder": {
                    "backend": "ollama_cloud",
                    "default": False,
                    "synthetic": False,
                    "runtime": {
                        "context": {
                            "policy": "fit",
                            "configured_ctx": 262144,
                            "fitted_ctx": 131072,
                            "fit_attempts": 1,
                            "outcome": "fitted",
                        },
                    },
                    "profile": {"context_length": 131072},
                },
            },
        ],
        "sonder": {
            "api_version": 1,
            "backends": [{"name": "ollama_cloud", "capabilities": ["chat", "tools"]}],
        },
    }

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return json.dumps(si_models).encode("utf-8")

    def fake_open(req, timeout=5.0, allow_remote=None):
        seen_headers.append(dict(req.header_items()))
        return _Resp()

    client = OllamaCloudMetadata("http://127.0.0.1:11437", opener=fake_open)
    result = client.show("kimi-k2.5:cloud")
    assert result.status == ProbeStatus.LIVE
    assert result.context_length == 131072
    assert seen_headers[0].get("Authorization") == "Bearer file-bearer-token-99"
