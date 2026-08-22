from datetime import datetime, timezone
from typing import get_type_hints

import pytest

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway_contract import (
    Capability,
    CapabilityHealth,
    GenerationChunk,
    GenerationMessage,
    GenerationRequest,
    GenerationResult,
    ModelGatewayProvider,
)


def test_generation_request_is_typed_and_immutable():
    request = GenerationRequest(
        model="local-model",
        messages=(GenerationMessage("user", "hello"),),
        options={"temperature": 0.2},
    )

    assert request.messages[0].role == "user"
    assert request.options["temperature"] == 0.2
    with pytest.raises((AttributeError, TypeError)):
        request.model = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: GenerationMessage("user", ""),
        lambda: GenerationRequest("", (GenerationMessage("user", "x"),)),
        lambda: GenerationRequest("m", ()),
    ],
)
def test_generation_inputs_reject_missing_required_values(factory):
    with pytest.raises((ValueError, TypeError)):
        factory()


def test_capability_health_is_explicit_and_queryable():
    health = CapabilityHealth(
        provider="fixture",
        capabilities=frozenset({Capability.GENERATION, Capability.STREAMING}),
        healthy=True,
        checked_at=datetime.now(timezone.utc),
    )

    assert health.supports(Capability.GENERATION)
    assert health.supports(Capability.STREAMING)
    assert CapabilityHealth("fixture", frozenset(), False, health.checked_at).supports(
        Capability.GENERATION
    ) is False


def test_provider_protocol_exposes_generation_streaming_and_health():
    methods = {"generate", "stream", "capability_health"}
    assert methods <= set(vars(ModelGatewayProvider))
    assert get_type_hints(ModelGatewayProvider.generate)["return"] is GenerationResult
    assert get_type_hints(ModelGatewayProvider.stream)["return"] is not None
    assert get_type_hints(ModelGatewayProvider.capability_health)["return"] is CapabilityHealth


class FixtureProvider:
    def generate(self, request, context):
        return GenerationResult("done", request.model, "stop", 1, 1)

    def stream(self, request, context):
        yield GenerationChunk("do")
        yield GenerationChunk("ne", "stop", 1, 1)

    def capability_health(self):
        return CapabilityHealth(
            "fixture",
            frozenset({Capability.GENERATION, Capability.STREAMING}),
            True,
            datetime.now(timezone.utc),
        )


def test_fixture_provider_satisfies_the_protocol_shape():
    provider: ModelGatewayProvider = FixtureProvider()
    request = GenerationRequest("fixture", (GenerationMessage("user", "hi"),))
    context = local_owner_context(correlation_id="wp3-contract")

    assert provider.generate(request, context).text == "done"
    assert [chunk.text for chunk in provider.stream(request, context)] == ["do", "ne"]
    assert provider.capability_health().supports(Capability.STREAMING)
