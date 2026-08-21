"""Model-gateway selection for the deterministic application graph.

Backend selection is an adapter boundary: it normalizes the operator's
backend setting and constructs the selected transport without making the
composition root own transport-specific policy.
"""
from __future__ import annotations

import os

from ..application.ports.model_gateway import ModelGateway
from .inference.ollama_gateway import OllamaGateway


_OPENAI_COMPATIBLE_BACKENDS = frozenset(
    {"openai", "openai-compatible", "llamacpp", "vllm"}
)


def build_model_gateway(
    *, target_resolver=None, generate_factory=None, embedding_provider=None,
    backend: str | None = None,
) -> ModelGateway:
    """Construct the configured model gateway.

    Ollama remains the default.  OpenAI-compatible aliases opt into the
    packaged OpenAI-compatible transport, whose own consent boundary remains
    authoritative for endpoint access.
    """
    selected_backend = (
        os.environ.get("SONDER_MODEL_BACKEND", "ollama")
        if backend is None else backend
    ).strip().lower()
    if selected_backend in _OPENAI_COMPATIBLE_BACKENDS:
        from .inference.openai_compat_gateway import OpenAICompatibleGateway

        return OpenAICompatibleGateway()
    return OllamaGateway(
        target_resolver=target_resolver,
        generate_factory=generate_factory,
        embedding_provider=embedding_provider,
    )


__all__ = ["build_model_gateway"]
