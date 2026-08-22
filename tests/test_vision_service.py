from __future__ import annotations

from pathlib import Path

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.vision_gateway import VisionInput, VisionResponse
from sonder_runtime.application.vision import VisionService


class Inputs:
    def load(self, path, context):
        return VisionInput(Path.cwd() / "image.png", b"bytes", "image/png", "a" * 64)


class Gateway:
    def analyze(self, request, context):
        assert request.image == b"bytes"
        assert request.media_type == "image/png"
        return VisionResponse("answer", "local-vlm", request.tier)


def test_vision_service_keeps_path_at_input_boundary():
    result = VisionService(Inputs(), Gateway()).analyze(
        "image.png", "describe", local_owner_context(correlation_id="vision-service")
    )
    assert result.text == "answer"
