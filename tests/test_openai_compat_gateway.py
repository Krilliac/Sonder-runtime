"""SPEC-3: OpenAICompatibleGateway — a second ModelGateway backend.

Offline tests via an injected transport; no network. Verifies the OpenAI
request/response shaping, the loopback-vs-cloud consent gate, single-attempt
error mapping into the domain taxonomy, and env-selected wiring in the graph.
"""
from __future__ import annotations

import socket
import urllib.error

import pytest

from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.domain.common.errors import (
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
)


def _ctx(*, cloud=False, timeout=30.0):
    return local_owner_context(
        correlation_id="req_oai", cloud_allowed=cloud, timeout_seconds=timeout
    )


def _local_cfg(**kw):
    return OpenAICompatibleConfig(base_url="http://127.0.0.1:8080", model="local-model", **kw)


def _chat_response(text, prompt_tokens=5, completion_tokens=7):
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def test_generate_shapes_request_and_parses_response():
    seen = {}

    def transport(url, payload, headers, timeout):
        seen["url"] = url
        seen["payload"] = payload
        return _chat_response("hello from llama.cpp")

    gw = OpenAICompatibleGateway(_local_cfg(), transport=transport)
    resp = gw.generate(ModelRequest(prompt="hi", tier="code", system="be terse"), _ctx())

    assert resp.text == "hello from llama.cpp"
    assert resp.tokens_in == 5 and resp.tokens_out == 7
    assert seen["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    roles = [m["role"] for m in seen["payload"]["messages"]]
    assert roles == ["system", "user"]
    assert seen["payload"]["messages"][-1]["content"] == "hi"


def test_reasoning_effort_is_nested_for_chat_template_servers():
    captured = {}

    def transport(url, payload, headers, timeout):
        captured["payload"] = payload
        return _chat_response("concise")

    OpenAICompatibleGateway(_local_cfg(), transport=transport).generate(
        ModelRequest(
            prompt="hi",
            tier="code",
            options={"reasoning_effort": "HIGH", "chat_template_kwargs": {"foo": "bar"}},
        ),
        _ctx(),
    )
    assert captured["payload"]["chat_template_kwargs"] == {
        "foo": "bar", "reasoning_effort": "high"
    }
    assert "reasoning_effort" not in captured["payload"]


def test_conflicting_template_reasoning_effort_is_rejected():
    gw = OpenAICompatibleGateway(_local_cfg(), transport=lambda *args: _chat_response("x"))
    with pytest.raises(InvalidInput, match="disagree"):
        gw.generate(
            ModelRequest(
                prompt="hi",
                tier="code",
                options={
                    "reasoning_effort": "low",
                    "chat_template_kwargs": {"reasoning_effort": "high"},
                },
            ),
            _ctx(),
        )


def test_generate_preserves_llama_cpp_timing_extension():
    def transport(*args):
        response = _chat_response("measured")
        response["timings"] = {
            "prompt_n": 12,
            "prompt_ms": 300.0,
            "predicted_n": 8,
            "predicted_ms": 400.0,
            "load_ms": 25.0,
            "prompt_per_second": 40.0,
            "predicted_per_second": 20.0,
        }
        response["cold_start"] = False
        return response

    response = OpenAICompatibleGateway(
        _local_cfg(), transport=transport
    ).generate(ModelRequest(prompt="hi", tier="code"), _ctx())
    assert response.telemetry.prompt_eval_ms == 300.0
    assert response.telemetry.eval_ms == 400.0
    assert response.telemetry.prompt_tokens_per_second == 40.0
    assert response.telemetry.output_tokens_per_second == 20.0
    assert response.telemetry.load_state == "warm"


def test_generate_does_not_invent_missing_timing():
    response = OpenAICompatibleGateway(
        _local_cfg(), transport=lambda *args: _chat_response("no extension")
    ).generate(ModelRequest(prompt="hi", tier="code"), _ctx())
    assert response.telemetry is None


def test_history_is_mapped_into_messages():
    captured = {}

    def transport(url, payload, headers, timeout):
        captured["messages"] = payload["messages"]
        return _chat_response("ok")

    gw = OpenAICompatibleGateway(_local_cfg(), transport=transport)
    history = ({"role": "user", "content": "prev"}, {"role": "assistant", "content": "ans"})
    gw.generate(ModelRequest(prompt="now", tier="code", history=history), _ctx())
    roles = [m["role"] for m in captured["messages"]]
    assert roles == ["user", "assistant", "user"]


def test_empty_prompt_is_invalid():
    gw = OpenAICompatibleGateway(_local_cfg(), transport=lambda *a: _chat_response("x"))
    with pytest.raises(InvalidInput):
        gw.generate(ModelRequest(prompt="   ", tier="code"), _ctx())


def test_missing_base_url_is_invalid():
    gw = OpenAICompatibleGateway(OpenAICompatibleConfig(base_url=""), transport=lambda *a: {})
    with pytest.raises(InvalidInput):
        gw.generate(ModelRequest(prompt="hi", tier="code"), _ctx())


def test_loopback_endpoint_needs_no_cloud_consent():
    gw = OpenAICompatibleGateway(_local_cfg(), transport=lambda *a: _chat_response("ok"))
    # cloud=False is fine for a loopback endpoint.
    assert gw.generate(ModelRequest(prompt="hi", tier="code"), _ctx(cloud=False)).text == "ok"


def test_non_loopback_endpoint_refused_without_cloud_consent():
    remote = OpenAICompatibleConfig(base_url="https://api.example.com", model="m")
    gw = OpenAICompatibleGateway(remote, transport=lambda *a: _chat_response("ok"))
    with pytest.raises(Forbidden):
        gw.generate(ModelRequest(prompt="hi", tier="code"), _ctx(cloud=False))
    # With explicit cloud consent it proceeds.
    assert gw.generate(ModelRequest(prompt="hi", tier="code"), _ctx(cloud=True)).text == "ok"


def test_http_auth_error_maps_to_forbidden():
    def transport(url, payload, headers, timeout):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    gw = OpenAICompatibleGateway(_local_cfg(), transport=transport)
    with pytest.raises(Forbidden):
        gw.generate(ModelRequest(prompt="hi", tier="code"), _ctx())


def test_timeout_maps_to_deadline_exceeded():
    def transport(url, payload, headers, timeout):
        raise socket.timeout("timed out")

    gw = OpenAICompatibleGateway(_local_cfg(), transport=transport)
    with pytest.raises(DeadlineExceeded):
        gw.generate(ModelRequest(prompt="hi", tier="code"), _ctx())


def test_connection_error_maps_to_dependency_unavailable():
    def transport(url, payload, headers, timeout):
        raise urllib.error.URLError("connection refused")

    gw = OpenAICompatibleGateway(_local_cfg(), transport=transport)
    with pytest.raises(DependencyUnavailable):
        gw.generate(ModelRequest(prompt="hi", tier="code"), _ctx())


def test_embed_round_trip_and_count_mismatch():
    def transport(url, payload, headers, timeout):
        n = len(payload["input"])
        return {"data": [{"embedding": [0.1, 0.2, 0.3]} for _ in range(n)]}

    gw = OpenAICompatibleGateway(_local_cfg(embed_model="emb"), transport=transport)
    out = gw.embed(["a", "b"], _ctx())
    assert len(out) == 2 and out[0].vector == (0.1, 0.2, 0.3) and out[0].model == "emb"

    def short(url, payload, headers, timeout):
        return {"data": [{"embedding": [0.1]}]}  # fewer than inputs

    gw2 = OpenAICompatibleGateway(_local_cfg(embed_model="emb"), transport=short)
    with pytest.raises(DependencyUnavailable):
        gw2.embed(["a", "b"], _ctx())


def test_embed_restores_input_order_from_response_indexes():
    def transport(url, payload, headers, timeout):
        return {
            "data": [
                {"index": 1, "embedding": [2.0]},
                {"index": 0, "embedding": [1.0]},
            ]
        }

    gateway = OpenAICompatibleGateway(_local_cfg(embed_model="emb"), transport=transport)
    result = gateway.embed(["first", "second"], _ctx())
    assert [item.vector for item in result] == [(1.0,), (2.0,)]


def test_graph_selects_backend_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "policy.json"))
    # Default: Ollama.
    monkeypatch.delenv("SONDER_MODEL_BACKEND", raising=False)
    bootstrap_app.reset_for_tests()
    from sonder_runtime.adapters.inference.ollama_gateway import OllamaGateway

    assert isinstance(bootstrap_app.build_application().model_gateway, OllamaGateway)
    # Opt-in: OpenAI-compatible.
    monkeypatch.setenv("SONDER_MODEL_BACKEND", "openai")
    bootstrap_app.reset_for_tests()
    assert isinstance(
        bootstrap_app.build_application().model_gateway, OpenAICompatibleGateway
    )
    bootstrap_app.reset_for_tests()
