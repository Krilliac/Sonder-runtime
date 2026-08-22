"""Injected local-only implementation of the application vision port."""
from __future__ import annotations

from ...application.context import OperationContext
from ...application.ports.vision_gateway import (
    VisionRequest,
    VisionResponse,
    require_vision_text,
)
from ...domain.common.errors import Cancelled, DeadlineExceeded, Forbidden, DependencyUnavailable


class InjectedVisionGateway:
    """Run a provider-owned local vision callable behind the typed port.

    The callable is injected by composition, so this adapter does not import
    the legacy server or choose a transport.  Remote/cloud consent flags are
    rejected rather than silently reinterpreted as local work.
    """

    def __init__(self, *, analyze, model: str = "local-vision"):
        if not callable(analyze):
            raise ValueError("vision analyze provider must be callable")
        if not str(model or "").strip():
            raise ValueError("vision model identity must not be empty")
        self._analyze_provider = analyze
        self._model = str(model)

    def analyze(self, request: VisionRequest, context: OperationContext) -> VisionResponse:
        if not isinstance(request, VisionRequest):
            raise TypeError("vision gateway requires VisionRequest")
        if context.expired:
            raise DeadlineExceeded("operation deadline exceeded before vision call")
        if context.cancellation is not None and context.cancellation.cancelled:
            raise Cancelled("operation cancelled before vision call")
        if context.cloud_allowed or context.remote_ollama_allowed:
            raise Forbidden("local vision gateway refuses cloud or remote consent")
        try:
            value = self._analyze_provider(request, context)
        except (Cancelled, DeadlineExceeded, Forbidden, DependencyUnavailable):
            raise
        except Exception as exc:
            raise DependencyUnavailable("local vision provider failed") from exc
        if isinstance(value, VisionResponse):
            return VisionResponse(
                text=require_vision_text(value.text),
                model=value.model or self._model,
                tier=value.tier or request.tier,
            )
        return VisionResponse(
            text=require_vision_text(value), model=self._model, tier=request.tier,
        )


__all__ = ["InjectedVisionGateway"]
