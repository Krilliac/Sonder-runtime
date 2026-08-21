"""Deterministic ModelGateway provider conformance gate ladder.

Gate 0: every registered adapter exposes the port methods.
Gate 1: request shaping preserves the common prompt/history/tier contract.
Gate 2: responses and optional usage accounting fail closed when malformed.
Gate 3: cancellation/deadlines stop abandoned work and bound supported drivers.
Gate 4: endpoint consent is exercised per provider below.

No test in this module opens a socket or invokes a model.
"""
from __future__ import annotations

import pytest

import sonder_runtime.adapters.embeddings as embeddings
import server
from sonder_runtime.adapters.ollama import endpoint as ollama_endpoint
from sonder_runtime.adapters.legacy_model_gateway import LegacyModelGateway
from sonder_runtime.adapters.ollama.gateway import OllamaGateway
from sonder_runtime.adapters.openai_compat.gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import (
    Cancelled,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
)
from tests.model_gateway_contract import (
    CancelledToken,
    GatewayContractProbe,
    ProviderCapabilities,
)

pytestmark = pytest.mark.unit

_REQUEST = ModelRequest(
    prompt="new question",
    tier="code",
    system="system instruction",
    history=(("user", "old question"), ("assistant", "old answer")),
    options={"temperature": 0.4, "num_predict": 17},
)


def _context(*, timeout=30.0, cancellation=None, cloud=False, remote=False):
    return local_owner_context(
        correlation_id="provider-contract",
        timeout_seconds=timeout,
        cancellation=cancellation,
        cloud_allowed=cloud,
        remote_ollama_allowed=remote,
    )


def _openai_probe(monkeypatch) -> GatewayContractProbe:
    calls = []
    response = {}
    embedding_response = {"data": []}

    def set_success(text, tokens_in, tokens_out):
        response.clear()
        response.update({
            "choices": [{"message": {"content": text}}],
            "usage": {
                "prompt_tokens": tokens_in,
                "completion_tokens": tokens_out,
            },
        })

    set_success("ok", 11, 7)

    def transport(url, payload, headers, timeout):
        calls.append({"url": url, "payload": payload, "timeout": timeout})
        if url.endswith("/v1/embeddings"):
            return embedding_response
        return response

    def set_embedding(value):
        embedding_response["data"] = [
            {"index": index, "embedding": value}
            for index in range(2)
        ]

    set_embedding([0.1, 0.2])

    gateway = OpenAICompatibleGateway(
        OpenAICompatibleConfig(
            base_url="http://127.0.0.1:8080", model="contract-model"
        ),
        transport=transport,
    )

    def captured():
        payload = calls[-1]["payload"]
        messages = payload["messages"]
        return {
            "prompt": messages[-1]["content"],
            "history": tuple(
                (row["role"], row["content"])
                for row in messages
                if row["role"] not in {"system"} and row is not messages[-1]
            ),
            "system": messages[0]["content"] if messages[0]["role"] == "system" else "",
            "tier": _REQUEST.tier,
            "temperature": payload["temperature"],
            "num_predict": payload["max_tokens"],
        }

    return GatewayContractProbe(
        name="openai-compatible",
        gateway=gateway,
        capabilities=ProviderCapabilities(True, True, True, True, False, True),
        generate=lambda context: gateway.generate(_REQUEST, context),
        embed=lambda context: list(gateway.embed(["first", "second"], context)),
        set_success=set_success,
        set_usage_object=lambda value: response.update(usage=value),
        set_embedding=set_embedding,
        captured_request=captured,
        captured_embedding_input=lambda: tuple(calls[-1]["payload"]["input"]),
        call_count=lambda: len(calls),
        transport_timeout=lambda: calls[-1]["timeout"] if calls else None,
        cooperative_cancel_hook=lambda: False,
    )


def _ollama_probe(monkeypatch) -> GatewayContractProbe:
    calls = []
    embedding_calls = []
    response = {"text": "ok", "tokens_in": 11, "tokens_out": 7}
    embedding_response = {"vector": [0.1, 0.2]}

    monkeypatch.setattr(
        server,
        "_serve_target",
        lambda tier, strict: ("contract-model", False, True, tier),
    )
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(embeddings, "BASE", "http://127.0.0.1:11434")

    def embed(text):
        embedding_calls.append(text)
        return embedding_response["vector"]

    monkeypatch.setattr(embeddings, "embed", embed)

    def set_success(text, tokens_in, tokens_out):
        response.update(text=text, tokens_in=tokens_in, tokens_out=tokens_out)

    def make_generate(
        model, system, temperature, num_predict, num_ctx,
        cloud=False, timeout=None, cancel_check=None, **kwargs
    ):
        call = {
            "model": model,
            "system": system,
            "temperature": temperature,
            "num_predict": num_predict,
            "timeout": timeout,
            "cancel_check": cancel_check,
        }
        calls.append(call)

        def generate(prompt, history=None):
            call["prompt"] = prompt
            call["history"] = tuple(tuple(row) for row in (history or ()))
            return response["text"]

        raw_usage = response.get("usage")
        generate.last_usage = raw_usage if "usage" in response else {
                "prompt_eval_count": response["tokens_in"],
                "eval_count": response["tokens_out"],
            }
        return generate

    monkeypatch.setattr(server, "_make_generate", make_generate)
    gateway = OllamaGateway()

    def captured():
        call = calls[-1]
        return {
            "prompt": call["prompt"],
            "history": call["history"],
            "system": call["system"],
            "tier": _REQUEST.tier,
            "temperature": call["temperature"],
            "num_predict": call["num_predict"],
        }

    return GatewayContractProbe(
        name="ollama",
        gateway=gateway,
        capabilities=ProviderCapabilities(True, True, True, True, True, True),
        generate=lambda context: gateway.generate(_REQUEST, context),
        embed=lambda context: list(gateway.embed(["first", "second"], context)),
        set_success=set_success,
        set_usage_object=lambda value: response.update(usage=value),
        set_embedding=lambda value: embedding_response.update(vector=value),
        captured_request=captured,
        captured_embedding_input=lambda: tuple(embedding_calls),
        call_count=lambda: len(calls),
        transport_timeout=lambda: calls[-1]["timeout"] if calls else None,
        cooperative_cancel_hook=lambda: bool(calls[-1]["cancel_check"]),
    )


def _legacy_probe(monkeypatch) -> GatewayContractProbe:
    calls = []
    embedding_calls = []
    response = {"text": "ok"}
    embedding_response = {"vector": [0.1, 0.2]}

    def sonder(prompt, history=None, tier=None):
        calls.append({"prompt": prompt, "history": history, "tier": tier})
        return response["text"]

    monkeypatch.setattr(server, "sonder", sonder)

    def embed(text):
        embedding_calls.append(text)
        return embedding_response["vector"]

    monkeypatch.setattr(embeddings, "embed", embed)

    def set_success(text, tokens_in, tokens_out):
        response["text"] = text

    gateway = LegacyModelGateway(
        generate=lambda prompt, *, history=None, tier=None: server.sonder(
            prompt, history=history, tier=tier
        )
    )
    return GatewayContractProbe(
        name="legacy-strangler",
        gateway=gateway,
        capabilities=ProviderCapabilities(False, False, False, False, False, False),
        generate=lambda context: gateway.generate(_REQUEST, context),
        embed=lambda context: list(gateway.embed(["first", "second"], context)),
        set_success=set_success,
        set_usage_object=lambda value: None,
        set_embedding=lambda value: embedding_response.update(vector=value),
        captured_request=lambda: {
            "prompt": calls[-1]["prompt"],
            "history": tuple(tuple(row) for row in calls[-1]["history"]),
            "tier": calls[-1]["tier"],
        },
        captured_embedding_input=lambda: tuple(embedding_calls),
        call_count=lambda: len(calls),
        transport_timeout=lambda: None,
        cooperative_cancel_hook=lambda: False,
    )


_PROVIDERS = [
    pytest.param(_ollama_probe, id="ollama"),
    pytest.param(_openai_probe, id="openai-compatible"),
    pytest.param(_legacy_probe, id="legacy-strangler"),
]


@pytest.fixture(params=_PROVIDERS)
def provider(request, monkeypatch):
    return request.param(monkeypatch)


def test_gate_0_adapter_exposes_the_model_gateway_port(provider):
    response = provider.generate(_context())
    assert response.text == "ok"
    assert callable(provider.gateway.generate)
    assert callable(provider.gateway.embed)


def test_gate_1_common_request_shape_is_preserved(provider):
    provider.generate(_context())
    captured = provider.captured_request()
    assert captured["prompt"] == _REQUEST.prompt
    assert captured["history"] == _REQUEST.history
    assert captured["tier"] == _REQUEST.tier
    if provider.capabilities.system_prompt:
        assert captured["system"] == _REQUEST.system
    if provider.capabilities.generation_options:
        assert captured["temperature"] == 0.4
        assert captured["num_predict"] == 17


def test_gate_1_embedding_request_shape_is_preserved(provider):
    provider.embed(_context())
    assert provider.captured_embedding_input() == ("first", "second")


def test_gate_2_usage_accounting_is_exact_when_supported(provider):
    response = provider.generate(_context())
    if provider.capabilities.usage_accounting:
        assert (response.tokens_in, response.tokens_out) == (11, 7)
    else:
        assert (response.tokens_in, response.tokens_out) == (None, None)


@pytest.mark.parametrize("malformed", [None, 17, "   "])
def test_gate_2_malformed_text_fails_closed(provider, malformed):
    provider.set_success(malformed, 11, 7)
    with pytest.raises(DependencyUnavailable):
        provider.generate(_context())


@pytest.mark.parametrize("malformed", [-1, True, "11"])
def test_gate_2_malformed_usage_fails_closed_when_supported(provider, malformed):
    if not provider.capabilities.usage_accounting:
        pytest.skip("adapter does not report provider usage")
    provider.set_success("ok", malformed, 7)
    with pytest.raises(DependencyUnavailable):
        provider.generate(_context())


def test_gate_2_malformed_usage_object_fails_closed_when_supported(provider):
    if not provider.capabilities.usage_accounting:
        pytest.skip("adapter does not report provider usage")
    provider.set_usage_object([])
    with pytest.raises(DependencyUnavailable):
        provider.generate(_context())


@pytest.mark.parametrize("malformed", [None, [], [float("nan")], [True], ["x"]])
def test_gate_2_malformed_embedding_fails_closed(provider, malformed):
    provider.set_embedding(malformed)
    with pytest.raises(DependencyUnavailable):
        provider.embed(_context())


def test_gate_3_cancelled_context_stops_before_provider_call(provider):
    before = provider.call_count()
    with pytest.raises(Cancelled):
        provider.generate(_context(cancellation=CancelledToken()))
    assert provider.call_count() == before


def test_gate_3_expired_context_stops_before_provider_call(provider):
    before = provider.call_count()
    with pytest.raises(DeadlineExceeded):
        provider.generate(_context(timeout=0.0))
    assert provider.call_count() == before


def test_gate_3_positive_subsecond_deadline_stays_bounded(provider):
    if not provider.capabilities.transport_deadline:
        pytest.skip("legacy adapter has no transport deadline seam")
    provider.generate(_context(timeout=0.2))
    timeout = provider.transport_timeout()
    assert timeout is not None and timeout > 0
    assert timeout <= 1.0


def test_gate_3_capability_matrix_is_explicit(provider):
    # A new adapter cannot silently inherit assumptions from another provider.
    assert isinstance(provider.capabilities, ProviderCapabilities)
    if provider.capabilities.cooperative_cancellation:
        provider.generate(_context())
        assert provider.cooperative_cancel_hook()


def test_gate_4_openai_remote_endpoint_requires_consent():
    calls = []
    gateway = OpenAICompatibleGateway(
        OpenAICompatibleConfig(base_url="https://provider.invalid", model="m"),
        transport=lambda *args: calls.append(args) or {
            "choices": [{"message": {"content": "ok"}}]
        },
    )
    request = ModelRequest(prompt="x", tier="code")
    with pytest.raises(Forbidden):
        gateway.generate(request, _context(cloud=False))
    assert calls == []
    assert gateway.generate(request, _context(cloud=True)).text == "ok"


def test_gate_4_remote_ollama_requires_consent(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(
        ollama_endpoint, "normalize", lambda value=None: "http://192.0.2.1:11434",
    )
    monkeypatch.setattr(
        server, "_serve_target", lambda tier, strict: ("m", False, True, "code")
    )

    def make_generate(*args, **kwargs):
        calls.append(kwargs)
        generate = lambda prompt, history=None: "ok"
        generate.last_usage = {}
        return generate

    monkeypatch.setattr(server, "_make_generate", make_generate)
    gateway = OllamaGateway()
    request = ModelRequest(prompt="x", tier="code")
    with pytest.raises(Forbidden):
        gateway.generate(request, _context(remote=False))
    assert calls == []
    assert gateway.generate(request, _context(remote=True)).text == "ok"
