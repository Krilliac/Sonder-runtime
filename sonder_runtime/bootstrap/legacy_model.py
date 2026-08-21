"""Explicit bootstrap composition for the transitional model provider."""
from __future__ import annotations

from types import ModuleType
import threading

from ..adapters.inference.ollama_gateway import OllamaGateway
from ..application.ports.model_target import ModelTarget


def configure_legacy_model_providers(runtime: ModuleType | None = None) -> None:
    """Bind legacy policy/transport only at startup composition time."""
    from .legacy_root import runtime as legacy_runtime

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


def lazy_legacy_model_provider_factories():
    """Return provider callables that defer the root compatibility import.

    Composition can therefore construct an application without loading the
    historical ``server`` module. The compatibility boundary is entered only
    when a model request actually needs the transitional provider.
    """
    lock = threading.Lock()
    providers: tuple[object, object] | None = None

    def ensure() -> tuple[object, object]:
        nonlocal providers
        if providers is None:
            with lock:
                if providers is None:
                    from .legacy_root import runtime as legacy_runtime

                    runtime = legacy_runtime()

                    def resolve(tier: str, strict: bool = False) -> ModelTarget:
                        model, cloud, augment, tier_label = runtime._serve_target(
                            tier, strict
                        )
                        return ModelTarget(model, cloud, tier_label, augment)

                    def generate(model, system, temperature, num_predict, num_ctx, **kwargs):
                        return runtime._make_generate(
                            model, system, temperature, num_predict, num_ctx, **kwargs
                        )

                    providers = (resolve, generate)
        return providers

    def resolve(tier: str, strict: bool = False) -> ModelTarget:
        return ensure()[0](tier, strict)  # type: ignore[operator]

    def generate(model, system, temperature, num_predict, num_ctx, **kwargs):
        return ensure()[1](model, system, temperature, num_predict, num_ctx, **kwargs)  # type: ignore[operator]

    return resolve, generate


__all__ = ["configure_legacy_model_providers", "lazy_legacy_model_provider_factories"]
