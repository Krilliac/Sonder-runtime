from __future__ import annotations

import pytest

from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import Embedding, ModelRequest, ModelResponse
from sonder_runtime.domain.common.errors import DependencyUnavailable, InvalidInput


class RecordingGateway:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.generated = []
        self.embedded = []

    def generate(self, request, context):
        self.generated.append((request, context))
        if self.fail:
            raise DependencyUnavailable(self.name + " unavailable")
        return ModelResponse(text=self.name, model=self.name, tier=request.tier)

    def embed(self, texts, context):
        values = tuple(texts)
        self.embedded.append((values, context))
        return [
            Embedding(vector=(float(i + 1),), model=self.name)
            for i in range(len(values))
        ]


def _context():
    return local_owner_context(correlation_id="provider-dispatch-test")


def _gateway(ollama, prism):
    return ProviderDispatchGateway(
        providers={"ollama": ollama, "openai_compatible": prism},
        tier_providers={
            "fast": "openai_compatible",
            "general": "openai_compatible",
            "code": "ollama",
            "reasoning": "ollama",
            "vision": "ollama",
        },
        default_generation_provider="ollama",
        embedding_provider="ollama",
    )


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("fast", "prism"),
        ("general", "prism"),
        ("code", "ollama"),
        ("reasoning", "ollama"),
    ],
)
def test_generation_uses_exact_tier_binding_and_forwards_identity(tier, expected):
    ollama = RecordingGateway("ollama")
    prism = RecordingGateway("prism")
    gateway = _gateway(ollama, prism)
    request = ModelRequest(prompt="hello", tier=tier)
    context = _context()

    response = gateway.generate(request, context)

    assert response.text == expected
    selected = prism if expected == "prism" else ollama
    assert selected.generated == [(request, context)]
    assert (ollama if selected is prism else prism).generated == []


def test_embeddings_ignore_generation_tiers_and_use_embedding_binding():
    ollama = RecordingGateway("ollama")
    prism = RecordingGateway("prism")
    gateway = _gateway(ollama, prism)
    context = _context()

    result = gateway.embed(["a", "b"], context)

    assert [row.model for row in result] == ["ollama", "ollama"]
    assert ollama.embedded == [(('a', 'b'), context)]
    assert prism.embedded == []


def test_sonder_tier_uses_the_configured_default_generation_provider():
    ollama = RecordingGateway("ollama")
    prism = RecordingGateway("prism")
    gateway = _gateway(ollama, prism)
    request = ModelRequest(prompt="hello", tier="sonder")
    context = _context()

    response = gateway.generate(request, context)

    assert response.text == "ollama"
    assert ollama.generated == [(request, context)]
    assert prism.generated == []


def test_caller_metadata_cannot_redirect_a_default_request_to_local_alias():
    ollama = RecordingGateway("ollama")
    prism = RecordingGateway("prism")
    gateway = ProviderDispatchGateway(
        providers={"ollama": ollama, "openai_compatible": prism},
        tier_providers={"fast": "ollama", "general": "ollama", "code": "ollama"},
        default_generation_provider="openai_compatible",
        embedding_provider="ollama",
    )
    request = ModelRequest("hello", "sonder", routing_metadata={"strict_alias": True})

    assert gateway.generate(request, _context()).model == "prism"
    assert len(prism.generated) == 1
    assert ollama.generated == []


def test_provider_failure_is_propagated_without_second_provider_call():
    ollama = RecordingGateway("ollama")
    prism = RecordingGateway("prism", fail=True)
    gateway = _gateway(ollama, prism)

    with pytest.raises(DependencyUnavailable, match="prism unavailable"):
        gateway.generate(ModelRequest(prompt="hello", tier="fast"), _context())

    assert len(prism.generated) == 1
    assert ollama.generated == []


def test_unknown_tier_and_missing_provider_fail_closed():
    ollama = RecordingGateway("ollama")
    prism = RecordingGateway("prism")
    gateway = _gateway(ollama, prism)
    with pytest.raises(InvalidInput, match="no provider binding"):
        gateway.generate(ModelRequest(prompt="hello", tier="oracle"), _context())

    with pytest.raises(InvalidInput, match="missing provider gateways"):
        ProviderDispatchGateway(
            providers={"ollama": ollama},
            tier_providers={"fast": "openai_compatible"},
            default_generation_provider="ollama",
            embedding_provider="ollama",
        )


def test_dispatch_rejects_caller_route_options_before_provider_call():
    ollama = RecordingGateway("ollama")
    prism = RecordingGateway("prism")
    gateway = _gateway(ollama, prism)
    with pytest.raises(InvalidInput, match="cannot be supplied"):
        gateway.generate(
            ModelRequest("hello", "code", options={"_resolved_route": {
                "provider_id": "ollama", "model": "arbitrary-model", "cloud": False,
            }}),
            _context(),
        )
    assert ollama.generated == []


class StatusGateway(RecordingGateway):
    def __init__(self, name, entry):
        super().__init__(name)
        self.entry = entry
        self.health_calls = 0

    def provider_status(self):
        return {self.entry["provider"]: dict(self.entry)}

    def capability_health(self):
        self.health_calls += 1
        return ("health", self.name)


def test_provider_status_aggregates_and_marks_silent_providers_unknown():
    reporting = StatusGateway("prism", {"provider": "openai_compatible", "state": "ready"})
    gateway = _gateway(RecordingGateway("ollama"), reporting)
    assert gateway.provider_status() == {
        "ollama": {"provider": "ollama", "state": "unknown"},
        "openai_compatible": {"provider": "openai_compatible", "state": "ready"},
    }


def test_capability_health_passes_through_to_one_provider():
    reporting = StatusGateway("prism", {"provider": "openai_compatible", "state": "ready"})
    gateway = _gateway(RecordingGateway("ollama"), reporting)
    assert gateway.capability_health("openai_compatible") == ("health", "prism")
    with pytest.raises(InvalidInput, match="does not report capability health"):
        gateway.capability_health()  # the default (ollama) reports none
    with pytest.raises(InvalidInput, match="not configured"):
        gateway.capability_health("vllm")
    assert reporting.generated == []
