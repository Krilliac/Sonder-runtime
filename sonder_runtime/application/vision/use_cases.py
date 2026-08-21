"""Vision application service joining guarded inputs to local inference."""
from __future__ import annotations

from ..context import OperationContext
from ..ports.vision_gateway import (
    VisionGateway,
    VisionInputProvider,
    VisionRequest,
    VisionResponse,
)


class VisionService:
    """Analyze a guarded image without exposing paths to the model gateway."""

    def __init__(self, inputs: VisionInputProvider, gateway: VisionGateway):
        self._inputs = inputs
        self._gateway = gateway

    def analyze(
        self, path: str, prompt: str, context: OperationContext, *, tier: str = "vision"
    ) -> VisionResponse:
        image = self._inputs.load(path, context)
        request = VisionRequest(
            prompt=prompt,
            image=image.image,
            media_type=image.media_type,
            tier=tier,
        )
        return self._gateway.analyze(request, context)


__all__ = ["VisionService"]
