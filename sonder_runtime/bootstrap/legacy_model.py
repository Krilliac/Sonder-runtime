"""Explicit bootstrap composition for the transitional model provider."""
from __future__ import annotations

from types import ModuleType
import threading

from ..adapters.inference.ollama_gateway import OllamaGateway
from ..adapters.model_bootstrap import (
    LegacyModelBootstrapAdapter,
    require_model_bootstrap_provider,
)
from ..application.ports.model_bootstrap import (
    LegacyModelRuntime,
    ModelBootstrapProvider,
)
from ..application.ports.model_target import ModelTarget


def configure_legacy_model_providers(
    runtime: ModuleType | None = None,
    *,
    provider: ModelBootstrapProvider | None = None,
) -> None:
    """Install typed model providers at the explicit compatibility boundary."""
    selected = _provider(runtime, provider)

    OllamaGateway.configure_default_providers(
        target_resolver=selected.resolve_target,
        generate_factory=selected.make_generate,
    )


def _legacy_runtime(runtime: ModuleType | None = None) -> LegacyModelRuntime:
    """Bind legacy policy/transport only at startup composition time."""
    from .legacy_root import runtime as legacy_runtime

    return runtime or legacy_runtime()


def _provider(
    runtime: ModuleType | None = None,
    provider: ModelBootstrapProvider | None = None,
) -> ModelBootstrapProvider:
    if provider is not None:
        return require_model_bootstrap_provider(provider)
    return LegacyModelBootstrapAdapter(_legacy_runtime(runtime))


def lazy_legacy_model_provider_factories(
    runtime: ModuleType | None = None,
    *,
    provider: ModelBootstrapProvider | None = None,
):
    """Return provider callables that defer the root compatibility import.

    Composition can therefore construct an application without loading the
    historical ``server`` module. The compatibility boundary is entered only
    when a model request actually needs the transitional provider.
    """
    lock = threading.Lock()
    selected: ModelBootstrapProvider | None = (
        require_model_bootstrap_provider(provider)
        if provider is not None
        else None
    )

    def ensure() -> ModelBootstrapProvider:
        nonlocal selected
        if selected is None:
            with lock:
                if selected is None:
                    selected = _provider(runtime)
        return require_model_bootstrap_provider(selected)

    def resolve(tier: str, strict: bool = False) -> ModelTarget:
        return ensure().resolve_target(tier, strict)

    def generate(model, system, temperature, num_predict, num_ctx, **kwargs):
        return ensure().make_generate(
            model, system, temperature, num_predict, num_ctx, **kwargs
        )

    return resolve, generate


__all__ = ["configure_legacy_model_providers", "lazy_legacy_model_provider_factories"]
