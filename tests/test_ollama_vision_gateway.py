from __future__ import annotations

import base64

import pytest

from sonder_runtime.adapters.inference.ollama_vision import OllamaVisionGateway
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_target import ModelTarget
from sonder_runtime.application.ports.vision_gateway import VisionRequest
from sonder_runtime.domain.common.errors import Forbidden


def _target(tier, strict):
    assert tier == "vision" and strict is True
    return ModelTarget("llava-local", False, "vision")


def test_ollama_vision_gateway_sends_bounded_image_payload(monkeypatch):
    monkeypatch.setattr(
        "sonder_runtime.adapters.inference.ollama_vision.ollama_endpoint.normalize",
        lambda: "http://127.0.0.1:11434",
    )
    monkeypatch.setattr(
        "sonder_runtime.adapters.inference.ollama_vision.ollama_endpoint.is_loopback",
        lambda value: True,
    )
    seen = {}

    def transport(url, payload, timeout):
        seen.update(payload=payload, url=url, timeout=timeout)
        return {"message": {"content": "looks local"}}

    result = OllamaVisionGateway(target_resolver=_target, transport=transport).analyze(
        VisionRequest("describe", b"pixels", "image/png"),
        local_owner_context(correlation_id="ollama-vision"),
    )
    assert result.text == "looks local"
    assert seen["payload"]["model"] == "llava-local"
    assert base64.b64decode(seen["payload"]["messages"][1]["images"][0]) == b"pixels"


def test_ollama_vision_gateway_refuses_remote_endpoint(monkeypatch):
    monkeypatch.setattr(
        "sonder_runtime.adapters.inference.ollama_vision.ollama_endpoint.normalize",
        lambda: "https://models.example.test",
    )
    monkeypatch.setattr(
        "sonder_runtime.adapters.inference.ollama_vision.ollama_endpoint.is_loopback",
        lambda value: False,
    )
    gateway = OllamaVisionGateway(target_resolver=_target, transport=lambda *args: {})
    with pytest.raises(Forbidden):
        gateway.analyze(
            VisionRequest("describe", b"pixels", "image/png"),
            local_owner_context(correlation_id="remote"),
        )
