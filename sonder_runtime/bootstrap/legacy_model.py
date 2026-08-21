"""Explicit bootstrap composition for the transitional model provider."""
from __future__ import annotations

from ..adapters.inference.ollama_gateway import OllamaGateway
from ..application.ports.model_target import ModelTarget


def configure_legacy_model_providers() -> None:
    """Bind legacy policy/transport only at startup composition time."""
    import server

    def resolve(tier: str, strict: bool = False) -> ModelTarget:
        model, cloud, augment, tier_label = server._serve_target(tier, strict)
        return ModelTarget(model, cloud, tier_label, augment)

    def generate(model, system, temperature, num_predict, num_ctx, **kwargs):
        return server._make_generate(
            model, system, temperature, num_predict, num_ctx, **kwargs
        )

    OllamaGateway.configure_default_providers(
        target_resolver=resolve, generate_factory=generate
    )


__all__ = ["configure_legacy_model_providers"]
