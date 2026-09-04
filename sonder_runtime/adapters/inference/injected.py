"""ModelGateway adapter for an explicitly injected provider.

The old provider is still supported, but its chat and embedding callables are
owned by the composition root/provider wiring.  This adapter never imports
the root ``server`` module.
"""
from __future__ import annotations

import logging
import time
import importlib

logger = logging.getLogger(__name__)

from ...application.context import OperationContext
from ...application.ports.model_gateway import (
    Embedding,
    ModelRequest,
    ModelResponse,
    require_embedding_vector,
    require_model_text,
)
from ...domain.common.errors import (
    Cancelled,
    DeadlineExceeded,
    DependencyUnavailable,
    InvalidInput,
)


def _check_context_liveness(
    context: OperationContext, *, phase: str = "before adapter call"
) -> None:
    """Reject expired or cancelled work at an adapter boundary."""
    if context.expired:
        raise DeadlineExceeded(f"operation deadline exceeded {phase}")
    if context.cancellation is not None and context.cancellation.cancelled:
        raise Cancelled(f"operation cancelled {phase}")


class InjectedModelGateway:
    """ModelGateway over explicitly injected provider callables."""

    def __init__(self, *, generate=None, embed=None):
        self._generate_provider = generate
        self._embedding_provider = embed
        logger.info(
            f"InjectedModelGateway initialized, "
            f"generate={'provided' if generate else 'none'}, "
            f"embed={'provided' if embed else 'none'}"
        )

    def generate(
        self, request: ModelRequest, context: OperationContext
    ) -> ModelResponse:
        if not (request.prompt or "").strip():
            raise InvalidInput("model request prompt is empty")
        _check_context_liveness(context)
        if self._generate_provider is None:
            raise DependencyUnavailable(
                "injected model gateway requires a generate provider"
            )
        logger.debug(f"InjectedModelGateway.generate: tier={request.tier!r}")
        started = time.monotonic()
        text = self._generate_provider(
            request.prompt,
            history=list(request.history) or None,
            tier=request.tier or None,
        )
        # A synchronous compatibility provider cannot always be interrupted in
        # flight.  Re-check before publishing its result so a cancellation race
        # cannot revive work the caller has already abandoned.
        _check_context_liveness(context, phase="during adapter call")
        duration_ms = int((time.monotonic() - started) * 1000)
        if duration_ms > 60_000:
            logger.warning(
                f"slow inference: injected provider took {duration_ms}ms "
                f"(>{60_000}ms threshold), tier={request.tier!r}"
            )
        logger.debug(f"InjectedModelGateway.generate: completed in {duration_ms}ms")
        return ModelResponse(
            text=require_model_text(text),
            model=request.tier or "sonder",
            tier=request.tier or "general",
            duration_ms=duration_ms,
        )

    def embed(self, texts, context: OperationContext):
        logger.debug(f"InjectedModelGateway.embed: text_count={len(list(texts)) if hasattr(texts, '__len__') else 'unknown'}")
        provider = self._embedding_provider
        if provider is None:
            logger.warning(
                "no embedding provider injected, falling back to default "
                "sonder_runtime.adapters.embeddings module"
            )
            provider = importlib.import_module(
                "sonder_runtime.adapters.embeddings"
            ).embed

        results = []
        for text in texts:
            _check_context_liveness(context)
            vector = provider(text)
            _check_context_liveness(context, phase="during adapter call")
            results.append(
                Embedding(
                    vector=require_embedding_vector(vector),
                    model="local",
                )
            )
        return results
