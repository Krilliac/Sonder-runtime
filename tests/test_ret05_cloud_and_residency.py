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
    """BLOCKING (2): real SI /v1/models envelope {'object':'list','data':[...],'sonder':{...}} works everywhere."""
    from sonder_runtime.domain.model_catalog import catalog_names, catalog_records, installed_records
    from sonder_runtime.adapters.model_inventory import inventory_rows
    from sonder_runtime.adapters.inference import ollama_pool

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
                    "runtime": {"context_tokens": 32768},
                },
            },
            {
                "id": "nomic-embed-text:latest",
                "object": "model",
                "owned_by": "sonder-inference",
                "sonder": {"backend": "llamacpp", "default": False, "synthetic": False},
            },
        ],
        "sonder": {"api_version": 1},
    }
    assert catalog_names(si_models) == ["nomic-embed-text:latest", "qwen3.6:35b"]
    recs = catalog_records(si_models)
    assert [name for name, _ in recs] == ["nomic-embed-text:latest", "qwen3.6:35b"]
    assert recs[1][1]["name"] == "qwen3.6:35b"
    inst = installed_records(si_models)
    assert [name for name, _ in inst] == ["qwen3.6:35b", "nomic-embed-text:latest"]

    inv = inventory_rows(si_models, "/v1/models")
    assert len(inv) == 2
    assert inv[0]["name"] == "qwen3.6:35b"

    assert ollama_pool._model_names(si_models) == ("nomic-embed-text:latest", "qwen3.6:35b")

    # Prober parses SI /v1/models data[] and extracts per-model runtime context cleanly.
    si_health = {
        "status": "ready",
        "api_version": 1,
        "version": "1.0.0",
        "models": [{"id": "qwen3.6:35b", "state": "resident", "context_length": 32768}],
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
    assert "qwen3.6:35b" in result["loaded_models"]
    assert result["model_contexts"]["qwen3.6:35b"] == 32768
