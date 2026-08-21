"""Explicit bootstrap composition for the transitional model provider."""
from __future__ import annotations

from types import ModuleType

from ..adapters.inference.ollama_gateway import OllamaGateway
from ..application.ports.model_target import ModelTarget
from .legacy_root import runtime as legacy_runtime


def configure_legacy_model_providers(runtime: ModuleType | None = None) -> None:
    """Bind legacy policy/transport only at startup composition time."""
    runtime = runtime or legacy_runtime()

    def resolve(tier: str, strict: bool = False) -> ModelTarget:
        model, cloud, augment, tier_label = runtime._serve_target(tier, strict)
        return ModelTarget(model, cloud, tier_label, augment)

    def generate(model, system, temperature, num_predict, num_ctx, **kwargs):
        return runtime._make_generate(
            model, system, temperature, num_predict, num_ctx, **kwargs
        )

    OllamaGateway.configure_default_providers(
        target_resolver=resolve, generate_factory=generate
    )


__all__ = ["configure_legacy_model_providers"]
