"""Compatibility ModelGateway adapter for the legacy server transport.

This adapter is intentionally separate from the strangler compatibility
module.  It keeps the old ``server.sonder`` and embedding entry points behind
the application port while newer deployments can select the packaged Ollama
or OpenAI-compatible gateways.
"""
from __future__ import annotations

import time

from ..application.context import OperationContext
from ..application.ports.model_gateway import (
    Embedding,
    ModelRequest,
    ModelResponse,
    require_embedding_vector,
    require_model_text,
)
from ..domain.common.errors import Cancelled, DeadlineExceeded, InvalidInput


def _check_context_liveness(context: OperationContext) -> None:
    """Reject expired or cancelled work before entering a legacy adapter."""
    if context.expired:
        raise DeadlineExceeded("operation deadline exceeded before adapter call")
    if context.cancellation is not None and context.cancellation.cancelled:
        raise Cancelled("operation cancelled before adapter call")


class LegacyModelGateway:
    """ModelGateway over the root server module's chat entry point."""

    def generate(
        self, request: ModelRequest, context: OperationContext
    ) -> ModelResponse:
        import server

        if not (request.prompt or "").strip():
            raise InvalidInput("model request prompt is empty")
        _check_context_liveness(context)
        started = time.monotonic()
        text = server.sonder(
            request.prompt,
            history=list(request.history) or None,
            tier=request.tier or None,
        )
        return ModelResponse(
            text=require_model_text(text),
            model=request.tier or "sonder",
            tier=request.tier or "general",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def embed(self, texts, context: OperationContext):
        import sonder_runtime.adapters.embeddings as embeddings

        results = []
        for text in texts:
            _check_context_liveness(context)
            results.append(Embedding(
                vector=require_embedding_vector(embeddings.embed(text)),
                model="local",
            ))
        return results
