"""Shared provider-gateway construction for both composition roots."""
from __future__ import annotations

from collections.abc import Callable, Mapping

from ..adapters.provider_dispatch.gateway import ProviderDispatchGateway
from ..application.ports.model_gateway import ModelGateway
from .provider_bindings import ProviderBindings

ProviderFactory = Callable[[], ModelGateway]


def _default_factories() -> dict[str, ProviderFactory]:
    from ..adapters.ollama.gateway import OllamaGateway
    from ..adapters.openai_compat.gateway import OpenAICompatibleGateway

    return {
        "ollama": OllamaGateway,
        "openai_compatible": OpenAICompatibleGateway,
    }


def build_model_gateway(
    bindings: ProviderBindings,
    provider_factories: Mapping[str, ProviderFactory] | None = None,
) -> ModelGateway:
    factories = dict(
        _default_factories() if provider_factories is None else provider_factories
    )
    missing = sorted(bindings.required_providers - set(factories))
    if missing:
        raise ValueError("missing provider factories: %s" % ", ".join(missing))
    gateways = {
        provider: factories[provider]() for provider in sorted(bindings.required_providers)
    }
    if len(gateways) == 1:
        return next(iter(gateways.values()))
    return ProviderDispatchGateway(
        providers=gateways,
        tier_providers=bindings.tier_providers,
        embedding_provider=bindings.embedding_provider,
    )
