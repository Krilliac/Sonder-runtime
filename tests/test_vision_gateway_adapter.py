from __future__ import annotations

import pytest

from sonder_runtime.adapters.inference.vision import InjectedVisionGateway
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.vision_gateway import VisionRequest, VisionResponse
from sonder_runtime.domain.common.errors import Cancelled, Forbidden


def _request():
    return VisionRequest("what is shown?", b"pixels", "image/png")


def test_injected_vision_gateway_returns_typed_response():
    gateway = InjectedVisionGateway(analyze=lambda request, context: "a red square")
    result = gateway.analyze(_request(), local_owner_context(correlation_id="vision"))
    assert isinstance(result, VisionResponse)
    assert result.text == "a red square"
    assert result.tier == "vision"


def test_injected_vision_gateway_rejects_remote_or_cloud_context():
    gateway = InjectedVisionGateway(analyze=lambda request, context: "never")
    with pytest.raises(Forbidden):
        gateway.analyze(
            _request(),
            local_owner_context(correlation_id="remote", remote_ollama_allowed=True),
        )
    with pytest.raises(Forbidden):
        gateway.analyze(
            _request(),
            local_owner_context(correlation_id="cloud", cloud_allowed=True),
        )


def test_injected_vision_gateway_honors_cancellation():
    class Token:
        cancelled = True

    gateway = InjectedVisionGateway(analyze=lambda request, context: "never")
    with pytest.raises(Cancelled):
        gateway.analyze(
            _request(),
            local_owner_context(correlation_id="cancelled", cancellation=Token()),
        )
