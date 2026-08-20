"""Compatibility ModelGateway adapter for an injected legacy provider.

The old provider is still supported, but its chat and embedding callables are
owned by the composition root/provider wiring.  This adapter never imports
the root ``server`` module.
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
from ..domain.common.errors import (
    Cancelled,
    DeadlineExceeded,
    DependencyUnavailable,
    InvalidInput,
)


def _check_context_liveness(context: OperationContext) -> None:
    """Reject expired or cancelled work before entering a legacy adapter."""
    if context.expired:
        raise DeadlineExceeded("operation deadline exceeded before adapter call")
    if context.cancellation is not None and context.cancellation.cancelled:
        raise Cancelled("operation cancelled before adapter call")


class LegacyModelGateway:
    """ModelGateway over explicitly injected legacy provider callables."""

    def __init__(self, *, generate=None, embed=None):
        self._generate_provider = generate
        self._embedding_provider = embed

    def generate(
        self, request: ModelRequest, context: OperationContext
    ) -> ModelResponse:
        if not (request.prompt or "").strip():
            raise InvalidInput("model request prompt is empty")
        _check_context_liveness(context)
        if self._generate_provider is None:
            raise DependencyUnavailable(
                "legacy model gateway requires an injected generate provider"
            )
        started = time.monotonic()
        text = self._generate_provider(
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
        provider = self._embedding_provider
        if provider is None:
            import sonder_runtime.adapters.embeddings as embeddings

            provider = embeddings.embed

        results = []
        for text in texts:
            _check_context_liveness(context)
            results.append(Embedding(
                vector=require_embedding_vector(provider(text)),
                model="local",
            ))
        return results
