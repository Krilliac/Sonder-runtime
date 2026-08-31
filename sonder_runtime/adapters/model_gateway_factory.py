"""Model-gateway composition for deterministic application graphs.

Provider selection is an adapter boundary: it normalizes operator bindings,
constructs only the required transports, and returns a direct gateway for a
uniform configuration or an exact tier dispatcher for a mixed configuration.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

from ..application.ports.model_gateway import ModelGateway
from ..domain.common.errors import InvalidInput
from .inference.ollama_gateway import OllamaGateway
from .provider_bindings import ProviderBindings, provider_bindings_from_env
from .provider_dispatch.gateway import ProviderDispatchGateway

ProviderFactory = Callable[[], ModelGateway]


def build_model_gateway(
    bindings: ProviderBindings | None = None,
    provider_factories: Mapping[str, ProviderFactory] | None = None,
    *, target_resolver=None, generate_factory=None, embedding_provider=None,
    backend: str | None = None,
) -> ModelGateway:
    """Construct the configured direct or tier-dispatching model gateway.

    Ollama remains the default. OpenAI-compatible aliases opt into the packaged
    transport, whose own consent boundary remains authoritative. Unknown names
    and incomplete factory maps fail closed rather than changing transport.
    """
    if bindings is not None and backend is not None:
        raise InvalidInput("bindings and backend cannot both be supplied")
    try:
        selected = (
            bindings
            if bindings is not None
            else ProviderBindings.uniform(backend)
            if backend is not None
            else provider_bindings_from_env()
        )
    except ValueError as exc:
        raise InvalidInput(str(exc)) from exc

    if provider_factories is None:
        from .inference.openai_compat_gateway import OpenAICompatibleGateway

        factories: dict[str, ProviderFactory] = {
            "ollama": lambda: OllamaGateway(
                target_resolver=target_resolver,
                generate_factory=generate_factory,
                embedding_provider=embedding_provider,
            ),
            "openai_compatible": OpenAICompatibleGateway,
        }
    else:
        factories = dict(provider_factories)

    missing = sorted(selected.required_providers - set(factories))
    if missing:
        raise InvalidInput("missing provider factories: %s" % ", ".join(missing))
    gateways = {
        provider: factories[provider]()
        for provider in sorted(selected.required_providers)
    }
    if len(gateways) == 1:
        return next(iter(gateways.values()))
    return ProviderDispatchGateway(
        providers=gateways,
        tier_providers=selected.tier_providers,
        default_generation_provider=selected.default_generation_provider,
        embedding_provider=selected.embedding_provider,
    )


__all__ = ["build_model_gateway"]
