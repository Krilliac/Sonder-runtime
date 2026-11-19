"""Model-gateway composition for deterministic application graphs.

Provider selection is an adapter boundary: it normalizes operator bindings,
constructs only the required transports, and returns a direct gateway for a
uniform configuration or an exact tier dispatcher for a mixed configuration.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from ..application.ports.model_gateway import ModelGateway
from ..domain.common.errors import InvalidInput
from .inference.ollama_gateway import OllamaGateway
from .provider_bindings import ProviderBindings, provider_bindings_from_env
from .provider_dispatch.gateway import ProviderDispatchGateway

if TYPE_CHECKING:
    from ..application.ports.model_target import (
        ModelGenerateFactory,
        ModelTargetResolver,
    )
    from .provider_dispatch.fallback import FallbackObserver

ProviderFactory = Callable[[], ModelGateway]


def build_model_gateway(
    bindings: ProviderBindings | None = None,
    provider_factories: Mapping[str, ProviderFactory] | None = None,
    *,
    target_resolver: ModelTargetResolver | None = None,
    generate_factory: ModelGenerateFactory | None = None,
    embedding_provider: object = None,
    backend: str | None = None,
    fallback_observer: FallbackObserver | None = None,
) -> ModelGateway:
    """Construct the configured direct or tier-dispatching model gateway.

    Ollama remains the default. OpenAI-compatible aliases opt into the packaged
    transport, whose own consent boundary remains authoritative. Sonder
    Inference aliases select ``SonderInferenceGateway``; ``openrouter`` selects
    the hosted ``OpenRouterGateway`` and ``anthropic`` the hosted
    ``AnthropicGateway`` (cloud consent enforced per call; each is
    constructed only when a binding names it). Unknown names and
    incomplete factory maps fail closed rather than changing transport.

    A declared fallback (only ``sonder_inference -> ollama``) wraps the
    primary in ``PreSendFallbackGateway``.  The fallback target is constructed
    once and shared with any direct binding of the same provider, but it is
    never exposed to tier dispatch unless it is also bound: a fallback is an
    outage path, not a routable provider.  ``fallback_observer`` receives
    ``(from_provider, to_provider, reason_code, context)`` per fallback.
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
        from .inference.anthropic_gateway import AnthropicGateway
        from .inference.openai_compat_gateway import OpenAICompatibleGateway
        from .inference.openrouter_gateway import OpenRouterGateway
        from .inference.sonder_inference_gateway import SonderInferenceGateway

        factories: dict[str, ProviderFactory] = {
            "ollama": lambda: OllamaGateway(
                target_resolver=target_resolver,
                generate_factory=generate_factory,
                embedding_provider=embedding_provider,
            ),
            "openai_compatible": OpenAICompatibleGateway,
            "sonder_inference": SonderInferenceGateway,
            "openrouter": OpenRouterGateway,
            "anthropic": AnthropicGateway,
        }
    else:
        factories = dict(provider_factories)

    missing = sorted(selected.constructed_providers - set(factories))
    if missing:
        raise InvalidInput("missing provider factories: %s" % ", ".join(missing))
    constructed = {
        provider: factories[provider]()
        for provider in sorted(selected.constructed_providers)
    }
    gateways = {
        provider: constructed[provider]
        for provider in sorted(selected.bound_providers)
    }
    if selected.fallbacks:
        from .provider_dispatch.fallback import PreSendFallbackGateway

        for primary, target in selected.fallbacks.items():
            gateways[primary] = PreSendFallbackGateway(
                constructed[primary],
                fallback=constructed[target],
                primary_id=primary,
                fallback_id=target,
                observer=fallback_observer,
            )
    if len(gateways) == 1:
        return next(iter(gateways.values()))
    return ProviderDispatchGateway(
        providers=gateways,
        tier_providers=selected.tier_providers,
        default_generation_provider=selected.default_generation_provider,
        embedding_provider=selected.embedding_provider,
    )


__all__ = ["build_model_gateway"]
