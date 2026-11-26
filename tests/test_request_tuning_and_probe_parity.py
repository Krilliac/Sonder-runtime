"""Parity tests for direct llamacpp request tuning and Sonder Inference protocol/capability probes."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sonder_runtime.adapters.inference import request_tuning
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceConfig,
    SonderInferenceGateway,
)
from sonder_runtime.adapters.inference.sonder_inference_probe import (
    CapabilityProbeResult,
    SonderInferenceIdentityReader,
    SonderInferenceProtocolProbe,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import Forbidden, InvalidInput
from sonder_runtime.domain.routing.backend_conformance import (
    BackendCapability,
    BackendIdentity,
)

IDENTITY = {
    "backend": "llamacpp",
    "model": "qwen3.8:27b",
    "model_digest": "a" * 64,
    "quantization": "Q4_K_M",
    "backend_version": "0.15.0",
    "tokenizer_digest": "b" * 64,
    "template_digest": "c" * 64,
    "context_tokens": 16384,
    "hardware": "gpu:rtx5070ti",
}

DIRECT_LLAMACPP_CAPS = [
    "tokenization",
    "streaming",
    "batched_prefill",
    "prefix_reuse",
    "speculative_decode",
    "structured_output",
    "embeddings",
    "deterministic",
    "recurrent_checkpoint",
    "logprobs",
]


def _llamacpp_health(
    *,
    backend_caps: list[str] | dict[str, object] | None = None,
    top_caps: list[str] | dict[str, object] | None = None,
    sonder_features: list[str] | dict[str, object] | None = None,
    available: bool = True,
    synthetic: bool = False,
    model: str = "qwen3.8:27b",
    include_backends: bool = True,
) -> dict[str, object]:
    doc: dict[str, object] = {
        "status": "ready",
        "api_version": 1,
        "version": "0.2.0",
        "synthetic": synthetic,
        "models": [{"id": model, "backend": "llamacpp", "default": True}],
        "sonder": {
            "api_version": 1,
            "features": [] if sonder_features is None else sonder_features,
            "pins": {"mode": "default"},
        },
    }
    if include_backends:
        backend_entry: dict[str, object] = {"name": "llamacpp", "available": available}
        if backend_caps is not None:
            backend_entry["capabilities"] = backend_caps
        doc["backends"] = [backend_entry]
    if top_caps is not None:
        doc["capabilities"] = top_caps
    return doc


def _identity_doc(*, model: str = "qwen3.8:27b", synthetic: bool = False, identity=IDENTITY) -> dict[str, object]:
    return {
        "schema": "sonder.inference.identity/1",
        "model": model,
        "synthetic": synthetic,
        "backend_identity": identity,
        "reason": None,
    }


class _TuningFake:
    def __init__(self, health: dict[str, object], model: str = "qwen3.8:27b") -> None:
        self.health = health
        self.model = model
        self.posts: list[dict[str, object]] = []

    def get(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        return 200, json.dumps(self.health).encode()

    def post(
        self, url: str, payload: dict[str, object], headers: dict[str, str], timeout: float,
    ) -> dict[str, object]:
        self.posts.append(json.loads(json.dumps(payload)))
        return {
            "model": self.model,
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            "sonder": {"api_version": 1},
        }


def _gateway_generate(
    health: dict[str, object],
    options: dict[str, object],
    *,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    fake = _TuningFake(health)
    gateway = SonderInferenceGateway(
        SonderInferenceConfig(model="default"),
        transport=fake.post,
        get_transport=fake.get,
        env=env or {},
        wall_clock=lambda: datetime(2026, 11, 25, tzinfo=timezone.utc),
    )
    gateway.generate(
        ModelRequest(prompt="hi", tier="general", options=options),
        local_owner_context(correlation_id="tune-test", source="http", timeout_seconds=10),
    )
    return fake.posts[0]


# -- Part 1: direct llamacpp request tuning & reasoning_effort --------------------


def test_direct_llamacpp_backends_capabilities_enable_think_and_reasoning_effort():
    health = _llamacpp_health(backend_caps=DIRECT_LLAMACPP_CAPS, sonder_features=[])
    assert request_tuning.thinking_supported(health, env={}) is True
    assert request_tuning.reasoning_effort_supported(health, env={}) is True

    post_true = _gateway_generate(health, {"think": True})
    assert post_true["chat_template_kwargs"] == {"enable_thinking": True}

    post_false = _gateway_generate(health, {"think": False})
    assert post_false["chat_template_kwargs"] == {"enable_thinking": False}

    post_high = _gateway_generate(health, {"reasoning_effort": "high"})
    assert post_high["chat_template_kwargs"] == {
        "enable_thinking": True,
        "reasoning_effort": "high",
    }

    post_off = _gateway_generate(health, {"reasoning_effort": "off"})
    assert post_off["chat_template_kwargs"] == {
        "enable_thinking": False,
        "reasoning_effort": "off",
    }


@pytest.mark.parametrize("health", [
    _llamacpp_health(top_caps=["structured_output", "embeddings", "logprobs"], backend_caps=[]),
    _llamacpp_health(
        top_caps={"structured_output": True, "embeddings": True, "logprobs": True},
        backend_caps=None,
    ),
    _llamacpp_health(
        top_caps={"structured_output": {"supported": True}},
        include_backends=False,
    ),
    _llamacpp_health(backend_caps=["prefix_reuse"]),
    {
        "status": "ready",
        "api_version": 1,
        "synthetic": False,
        "sonder": {"api_version": 1, "features": ["reasoning_effort"]},
    },
])
def test_direct_llamacpp_and_top_level_capability_shapes_support_thinking(health):
    assert request_tuning.thinking_supported(health, env={}) is True
    payload: dict[str, object] = {"model": "qwen3.8:27b"}
    request_tuning.tune_request(payload, None, health, env={}, reasoning_effort="medium")
    assert payload["chat_template_kwargs"] == {
        "enable_thinking": True,
        "reasoning_effort": "medium",
    }


@pytest.mark.parametrize("health", [
    _llamacpp_health(backend_caps=DIRECT_LLAMACPP_CAPS, synthetic=True),
    _llamacpp_health(backend_caps=DIRECT_LLAMACPP_CAPS, available=False),
    _llamacpp_health(backend_caps=[], sonder_features=[]),
    _llamacpp_health(backend_caps=["streaming"], sonder_features=[]),
    _llamacpp_health(
        backend_caps=DIRECT_LLAMACPP_CAPS,
        sonder_features={"thinking": False},
    ),
    _llamacpp_health(
        top_caps={"structured_output": True, "enable_thinking": False},
        backend_caps=[],
    ),
    _llamacpp_health(
        top_caps={"structured_output": {"supported": False}},
        backend_caps=[],
    ),
    {
        "status": "ready",
        "api_version": 1,
        "synthetic": False,
        "backends": [
            {"name": "llamacpp", "available": True, "capabilities": DIRECT_LLAMACPP_CAPS},
            {"name": "llamaserver", "available": True, "capabilities": ["streaming"]},
        ],
        "models": [{"id": "qwen3.8:27b", "backend": "llamaserver", "default": True}],
        "sonder": {"api_version": 1, "features": []},
    },
    {
        "status": "ready",
        "api_version": 1,
        "synthetic": True,
        "backends": [{"name": "mock", "available": True, "capabilities": DIRECT_LLAMACPP_CAPS}],
        "models": [{"id": "mock:tiny", "backend": "mock", "default": True}],
        "sonder": {"api_version": 1, "features": []},
    },
])
def test_fail_closed_when_direct_llamacpp_unavailable_synthetic_or_disabled(health):
    assert request_tuning.thinking_supported(health, env={}) is False
    with pytest.raises(InvalidInput, match="thinking"):
        _gateway_generate(health, {"think": True})
    post = _gateway_generate(health, {"reasoning_effort": "high"})
    assert "chat_template_kwargs" not in post


def test_env_thinking_off_overrides_direct_llamacpp_capabilities():
    health = _llamacpp_health(backend_caps=DIRECT_LLAMACPP_CAPS)
    assert request_tuning.thinking_supported(health, env={"SONDER_INFERENCE_THINKING": "off"}) is False
    with pytest.raises(InvalidInput, match="thinking"):
        _gateway_generate(health, {"think": True}, env={"SONDER_INFERENCE_THINKING": "off"})
    post = _gateway_generate(
        health, {"reasoning_effort": "high"}, env={"SONDER_INFERENCE_THINKING": "off"},
    )
    assert "chat_template_kwargs" not in post


def test_advertised_features_skips_unavailable_backend_and_reads_nested_supported_flags():
    doc = {
        "capabilities": {
            "embeddings": {"supported": True},
            "logprobs": {"supported": False},
        },
        "backends": [
            {"name": "llamacpp", "available": False, "capabilities": ["tool_calling"]},
            {"name": "llamaserver", "available": True, "features": ["structured_output"]},
        ],
    }
    feats = request_tuning.advertised_features(doc)
    assert "embeddings" in feats
    assert "structured_output" in feats
    assert "logprobs" not in feats
    assert "tool_calling" not in feats


def test_tune_request_reasoning_effort_validation_and_explicit_think_precedence():
    health = _llamacpp_health(backend_caps=DIRECT_LLAMACPP_CAPS)
    payload: dict[str, object] = {"model": "qwen3.8:27b"}
    request_tuning.tune_request(payload, False, health, env={}, reasoning_effort="LOW")
    assert payload["chat_template_kwargs"] == {
        "enable_thinking": False,
        "reasoning_effort": "low",
    }
    with pytest.raises(InvalidInput, match="reasoning_effort"):
        request_tuning.tune_request({"model": "qwen3.8:27b"}, None, health, env={}, reasoning_effort="turbo")


# -- Part 2: TOOL_* conformance & capability probing (/v1/embeddings, structured, logprobs) --


class _RichInferenceFake:
    """In-memory Sonder-Inference fixture advertising tool_calling, embeddings, structured_output, logprobs."""

    def __init__(
        self,
        *,
        model: str = "qwen3.8:27b",
        capabilities: list[str] | None = None,
        identity_doc: dict[str, object] | None = None,
    ) -> None:
        self.model = model
        self.capabilities = list(capabilities) if capabilities is not None else [
            "tokenization", "streaming", "structured_output", "embeddings", "tool_calling", "logprobs",
        ]
        self.identity_doc = identity_doc or _identity_doc(model=model)
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.custom_chat: dict[str, object] | None = None
        self.custom_embed: dict[str, object] | None = None
        self.chat_error: Exception | None = None
        self.embed_error: Exception | None = None

    def get(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        if "/v1/sonder/identity" in url:
            return 200, json.dumps(self.identity_doc).encode()
        health = _llamacpp_health(
            backend_caps=self.capabilities,
            top_caps=self.capabilities,
            model=self.model,
        )
        return 200, json.dumps(health).encode()

    def post(
        self, url: str, payload: dict[str, object], headers: dict[str, str], timeout: float,
    ) -> dict[str, object]:
        copied = json.loads(json.dumps(payload))
        self.posts.append((url, copied))
        if url.endswith("/v1/embeddings"):
            if self.embed_error is not None:
                raise self.embed_error
            if self.custom_embed is not None:
                return self.custom_embed
            return {
                "object": "list",
                "model": self.model,
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, -0.2, 0.3, 0.4]}],
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
                "sonder": {"api_version": 1},
            }
        if self.chat_error is not None:
            raise self.chat_error
        if self.custom_chat is not None:
            return self.custom_chat
        return self._handle_chat(copied)

    def _handle_chat(self, payload: dict[str, object]) -> dict[str, object]:
        messages = payload["messages"]
        assert isinstance(messages, list)
        last = messages[-1]
        assert isinstance(last, dict)
        content = str(last.get("content") or "")
        if payload.get("logprobs") is True:
            return {
                "model": self.model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                    "logprobs": {
                        "content": [{
                            "token": "ok",
                            "logprob": -0.05,
                            "bytes": [111, 107],
                            "top_logprobs": [
                                {"token": "ok", "logprob": -0.05, "bytes": [111, 107]},
                                {"token": "hi", "logprob": -2.1, "bytes": [104, 105]},
                            ],
                        }],
                    },
                }],
            }
        if payload.get("tools"):
            if last.get("role") == "tool":
                if content == "a":
                    return self._tool_response([("call_b", "echo", '{"value":"b"}')])
                return {
                    "model": self.model,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "Echoed alpha."},
                        "finish_reason": "stop",
                    }],
                }
            if "in parallel" in content:
                return self._tool_response([
                    ("call_p1", "echo", '{"value":"a"}'),
                    ("call_p2", "echo", '{"value":"b"}'),
                ])
            if "twice in sequence" in content:
                return self._tool_response([("call_a", "echo", '{"value":"a"}')])
            return self._tool_response([("call_1", "echo", '{"value":"alpha"}')])
        if "Return only a JSON object" in content:
            text = '{"tool":"echo","arguments":{"value":"alpha"}}'
        else:
            text = "hello"
        return {
            "model": self.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
        }

    def _tool_response(self, calls: list[tuple[str, str, str]]) -> dict[str, object]:
        return {
            "model": self.model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": cid,
                            "type": "function",
                            "function": {"name": name, "arguments": args},
                        }
                        for cid, name, args in calls
                    ],
                },
                "finish_reason": "tool_calls",
            }],
        }


def _probe_for(fake: _RichInferenceFake, *, base_url: str = "http://127.0.0.1:18437",
               cloud_allowed: bool = False) -> SonderInferenceProtocolProbe:
    gateway = SonderInferenceGateway(
        SonderInferenceConfig(base_url=base_url, model=fake.model, health_ttl_seconds=0),
        transport=fake.post,
        get_transport=fake.get,
    )
    reader = SonderInferenceIdentityReader(gateway, model=fake.model)
    return SonderInferenceProtocolProbe(gateway, identity_reader=reader, cloud_allowed=cloud_allowed)


def test_protocol_probe_passes_all_native_tool_and_capability_cases():
    fake = _RichInferenceFake()
    probe = _probe_for(fake)
    record = probe.run(timeout_seconds=10)
    by_cap = {item.capability: item for item in record.results}

    assert record.synthetic is True
    assert record.identity == BackendIdentity.from_dict(IDENTITY)
    assert by_cap[BackendCapability.CHAT].passed is True
    assert by_cap[BackendCapability.STRUCTURED].passed is True
    assert by_cap[BackendCapability.TOOL_NATIVE].passed is True
    assert by_cap[BackendCapability.TOOLS_WITH_SCHEMA].passed is True
    assert by_cap[BackendCapability.TOOL_SEQUENTIAL].passed is True
    assert by_cap[BackendCapability.TOOL_CONTINUATION].passed is True
    assert by_cap[BackendCapability.EMBEDDING].passed is True
    assert by_cap[BackendCapability.TOOL_FALLBACK].passed is None
    assert by_cap[BackendCapability.CANCELLATION].passed is None

    # Verify structured probe actually sent OpenAI response_format json_schema
    structured_posts = [p for url, p in fake.posts if url.endswith("/v1/chat/completions") and "response_format" in p and "tools" not in p]
    assert len(structured_posts) == 1
    assert structured_posts[0]["response_format"]["type"] == "json_schema"

    # Verify sequential & continuation turns used OpenAI tool_call_id role="tool" messages
    tool_turns = [
        m for _url, p in fake.posts
        for m in (p.get("messages") or [])
        if isinstance(m, dict) and m.get("role") == "tool"
    ]
    assert len(tool_turns) == 2
    assert all(m.get("tool_call_id") and m.get("name") == "echo" for m in tool_turns)

    # Verify last_capability_probes populated for all 4 capabilities
    caps = probe.last_capability_probes
    assert caps["embeddings"] == CapabilityProbeResult(
        "embeddings", True, True, "embeddings_passed", dimensions=4,
    )
    assert caps["structured_output"] == CapabilityProbeResult(
        "structured_output", True, True, "structured_output_passed",
    )
    assert caps["logprobs"] == CapabilityProbeResult("logprobs", True, True, "logprobs_passed")
    assert caps["tool_calling"] == CapabilityProbeResult(
        "tool_calling", True, True, "tool_calling_passed",
    )


def test_tool_parallel_case_validates_two_distinct_calls():
    fake = _RichInferenceFake()
    probe = _probe_for(fake)
    probe._begin_session(10.0)
    result = probe.run_case(BackendCapability.TOOL_PARALLEL, model=fake.model, timeout_seconds=10.0)
    assert result == {
        "model": fake.model,
        "events": ["call:a", "call:b", "result:a", "result:b"],
        "parallel_max": 2,
    }


def test_probe_capabilities_standalone_and_unadvertised_gating():
    unadv_fake = _RichInferenceFake(capabilities=["streaming"])
    unadv_probe = _probe_for(unadv_fake)
    unadv = unadv_probe.probe_capabilities(timeout_seconds=10)
    assert all(res.advertised is False and res.supported is None for res in unadv.values())
    assert unadv_fake.posts == []

    # force=True probes endpoints even when not advertised in health
    forced = unadv_probe.probe_capabilities(timeout_seconds=10, force=True)
    assert forced["embeddings"].supported is True and forced["embeddings"].advertised is False
    assert forced["structured_output"].supported is True
    assert forced["logprobs"].supported is True
    assert forced["tool_calling"].supported is True

    # Individual helpers work standalone too
    rich_fake = _RichInferenceFake()
    rich_probe = _probe_for(rich_fake)
    assert rich_probe.probe_embeddings(timeout_seconds=10).dimensions == 4
    assert rich_probe.probe_structured_output(timeout_seconds=10).supported is True
    assert rich_probe.probe_logprobs(timeout_seconds=10).supported is True


@pytest.mark.parametrize("bad_calls", [
    [],  # empty tool_calls
    [{"id": "c1", "type": "function", "function": {"name": "undeclared_tool", "arguments": '{"value":"alpha"}'}}],
    [{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"value":NaN}'}}],
    [{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"value":"alpha","extra":1}'}}],
    [{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": {"value": "alpha"}}}],  # non-string args
    [{"id": "", "type": "function", "function": {"name": "echo", "arguments": '{"value":"alpha"}'}}],  # empty id
    [{"id": "bad\x00id", "type": "function", "function": {"name": "echo", "arguments": '{"value":"alpha"}'}}],
    [{"id": "c1", "type": "other", "function": {"name": "echo", "arguments": '{"value":"alpha"}'}}],
    [{"id": f"c{i}", "type": "function", "function": {"name": "echo", "arguments": '{"value":"alpha"}'}} for i in range(10)],
])
def test_tool_native_probe_fails_closed_on_malformed_or_undeclared_tool_calls(bad_calls):
    fake = _RichInferenceFake()
    fake.custom_chat = {
        "model": fake.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": None, "tool_calls": bad_calls},
            "finish_reason": "tool_calls",
        }],
    }
    probe = _probe_for(fake)
    probe._begin_session(10.0)
    observed = probe.run_case(BackendCapability.TOOL_NATIVE, model=fake.model, timeout_seconds=10.0)
    assert observed == {"model": fake.model, "tool_calls": [], "schema_valid": False}


@pytest.mark.parametrize("bad_embed,expected_reason", [
    ({"model": "wrong-model", "data": [{"index": 0, "embedding": [0.1]}]}, "embeddings_model_mismatch"),
    ({"model": "qwen3.8:27b", "data": []}, "embeddings_invalid"),
    ({"model": "qwen3.8:27b", "data": [{"index": 0, "embedding": []}]}, "embeddings_invalid"),
    ({"model": "qwen3.8:27b", "data": [{"index": 0, "embedding": [float("nan")]}]}, "embeddings_invalid"),
    ({"model": "qwen3.8:27b", "data": [{"index": 0, "embedding": [float("inf")]}]}, "embeddings_invalid"),
    ({"model": "qwen3.8:27b", "data": [{"index": 0, "embedding": [True, False]}]}, "embeddings_invalid"),
    ({"model": "qwen3.8:27b", "data": [{"index": 1, "embedding": [0.1]}]}, "embeddings_invalid"),
    ({"model": "qwen3.8:27b", "data": [{"index": 0, "embedding": [0.1]}, {"index": 1, "embedding": [0.2]}]}, "embeddings_invalid"),
])
def test_embeddings_probe_fails_closed_on_invalid_vectors(bad_embed, expected_reason):
    fake = _RichInferenceFake()
    fake.custom_embed = bad_embed
    result = _probe_for(fake).probe_embeddings(timeout_seconds=10)
    assert result.advertised is True
    assert result.supported is False
    assert result.reason_code == expected_reason
    assert result.dimensions is None


@pytest.mark.parametrize("bad_logprobs,expected_reason", [
    (None, "logprobs_missing"),
    ({"content": []}, "logprobs_invalid"),
    ({"content": [{"token": "ok", "logprob": 0.25, "top_logprobs": [{"token": "ok", "logprob": -0.1}]}]}, "logprobs_invalid"),
    ({"content": [{"token": "ok", "logprob": float("nan"), "top_logprobs": [{"token": "ok", "logprob": -0.1}]}]}, "logprobs_invalid"),
    ({"content": [{"token": "ok", "logprob": -0.1, "top_logprobs": []}]}, "logprobs_invalid"),
    ({"content": [{"token": "ok", "logprob": -0.1, "bytes": [300], "top_logprobs": [{"token": "ok", "logprob": -0.1}]}]}, "logprobs_invalid"),
    ({"content": [{"token": "ok", "logprob": True, "top_logprobs": [{"token": "ok", "logprob": -0.1}]}]}, "logprobs_invalid"),
])
def test_logprobs_probe_fails_closed_on_missing_or_invalid_payloads(bad_logprobs, expected_reason):
    fake = _RichInferenceFake()
    fake.custom_chat = {
        "model": fake.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
            "logprobs": bad_logprobs,
        }],
    }
    result = _probe_for(fake).probe_logprobs(timeout_seconds=10)
    assert result.advertised is True
    assert result.supported is False
    assert result.reason_code == expected_reason


def test_structured_probe_rejects_nan_json_constants():
    fake = _RichInferenceFake()
    fake.custom_chat = {
        "model": fake.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": '{"tool":"echo","arguments":{"value":NaN}}'},
            "finish_reason": "stop",
        }],
    }
    result = _probe_for(fake).probe_structured_output(timeout_seconds=10)
    assert result.supported is False
    assert result.reason_code == "structured_output_invalid"


def test_probe_refuses_unconsented_remote_endpoint_before_any_request():
    fake = _RichInferenceFake()
    gateway = SonderInferenceGateway(
        SonderInferenceConfig(
            base_url="https://gpu-box.example:11437",
            model=fake.model,
            api_key="test-key-1234567890",
            allow_remote=True,
        ),
        transport=fake.post,
        get_transport=fake.get,
    )
    reader = SonderInferenceIdentityReader(gateway, model=fake.model)
    probe = SonderInferenceProtocolProbe(gateway, identity_reader=reader, cloud_allowed=False)
    with pytest.raises(Forbidden):
        probe.probe_capabilities(timeout_seconds=5)
    assert fake.posts == []


def test_probe_detects_identity_drift_during_capability_probe():
    fake = _RichInferenceFake()
    gateway = SonderInferenceGateway(
        SonderInferenceConfig(base_url="http://127.0.0.1:18437", model=fake.model, health_ttl_seconds=0),
        transport=fake.post,
        get_transport=fake.get,
    )
    reader = SonderInferenceIdentityReader(gateway, model=fake.model)
    probe = SonderInferenceProtocolProbe(gateway, identity_reader=reader)
    original_post = fake.post

    def rotating_post(url: str, payload: dict[str, object], headers: dict[str, str], timeout: float):
        fake.identity_doc = _identity_doc(
            model=fake.model, identity={**IDENTITY, "model_digest": "f" * 64},
        )
        return original_post(url, payload, headers, timeout)

    fake.post = rotating_post  # type: ignore[method-assign]
    gateway._transport = rotating_post
    with pytest.raises(ValueError, match="changed"):
        probe.probe_embeddings(timeout_seconds=5)


# -- Real loopback HTTP parity test ----------------------------------------------


class _LoopbackInferenceHandler(BaseHTTPRequestHandler):
    fake: _RichInferenceFake

    def log_message(self, *args: object) -> None:
        return

    def _send_json(self, status: int, body: dict[str, object]) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        status, raw = self.fake.get(self.path, {}, 5.0)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        body = self.fake.post(self.path, payload, {}, 5.0)
        self._send_json(200, body)


def test_loopback_http_end_to_end_probe_and_direct_llamacpp_tuning():
    fake = _RichInferenceFake()
    handler_cls = type("Handler", (_LoopbackInferenceHandler,), {"fake": fake})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = "http://127.0.0.1:%d" % httpd.server_address[1]
        gateway = SonderInferenceGateway(
            SonderInferenceConfig(base_url=base_url, model=fake.model, health_ttl_seconds=0),
            env={},
        )
        # 1. Direct llamacpp chat with think=True and reasoning_effort="high" over HTTP
        gateway.generate(
            ModelRequest(
                prompt="hi",
                tier="general",
                options={"think": True, "reasoning_effort": "high"},
            ),
            local_owner_context(correlation_id="loopback-tune", source="http", timeout_seconds=10),
        )
        chat_post = fake.posts[0][1]
        assert chat_post["chat_template_kwargs"] == {
            "enable_thinking": True,
            "reasoning_effort": "high",
        }

        # 2. Full protocol + capability probe over HTTP
        reader = SonderInferenceIdentityReader(gateway, model=fake.model)
        probe = SonderInferenceProtocolProbe(gateway, identity_reader=reader)
        record = probe.run(timeout_seconds=10)
        results = {item.capability: item for item in record.results}
        assert results[BackendCapability.CHAT].passed is True
        assert results[BackendCapability.STRUCTURED].passed is True
        assert results[BackendCapability.TOOL_NATIVE].passed is True
        assert results[BackendCapability.TOOLS_WITH_SCHEMA].passed is True
        assert results[BackendCapability.TOOL_SEQUENTIAL].passed is True
        assert results[BackendCapability.TOOL_CONTINUATION].passed is True
        assert results[BackendCapability.EMBEDDING].passed is True
        assert probe.last_capability_probes["logprobs"].supported is True
        assert probe.last_capability_probes["embeddings"].dimensions == 4
    finally:
        httpd.shutdown()
        httpd.server_close()
