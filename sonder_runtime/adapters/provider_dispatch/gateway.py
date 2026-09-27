"""Select exactly one configured ModelGateway for each request."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType

from ...application.context import OperationContext
from ...application.ports.model_gateway import (
    Embedding,
    ModelGateway,
    ModelRequest,
    ModelResponse,
)
from ...application.ports.model_target import ResolvedModelRoute
from ...domain.common.errors import InvalidInput


class ProviderDispatchGateway:
    def __init__(
        self,
        *,
        providers: Mapping[str, ModelGateway],
        tier_providers: Mapping[str, str],
        default_generation_provider: str,
        embedding_provider: str,
    ) -> None:
        provider_map = dict(providers)
        tier_map = dict(tier_providers)
        required = set(tier_map.values()) | {
            default_generation_provider,
            embedding_provider,
        }
        missing = sorted(required - set(provider_map))
        if missing:
            raise InvalidInput("missing provider gateways: %s" % ", ".join(missing))
        self._providers = MappingProxyType(provider_map)
        self._tier_providers = MappingProxyType(tier_map)
        self._default_generation_provider = default_generation_provider
        self._embedding_provider = embedding_provider
        self._route_issuer = object()

    def _provider_for_request(self, request: ModelRequest) -> str:
        provider = self._tier_providers.get(request.tier)
        if provider is None and request.tier == "sonder":
            provider = self._default_generation_provider
        if provider is None:
            raise InvalidInput("no provider binding for tier %r" % request.tier)
        return provider

    def generate_strict_alias(
        self, request: ModelRequest, context: OperationContext,
    ) -> ModelResponse:
        """Serve an operator-selected local alias via the configured Ollama gate.

        This is an explicit ChatService route; user-supplied ModelRequest
        metadata cannot override the normal provider binding.
        """
        if request.tier != "sonder" or "ollama" not in self._providers:
            raise InvalidInput("strict chat alias requires a configured local Ollama provider")
        if "_resolved_route" in (request.options or {}) or request._resolved_route is not None:
            raise InvalidInput("strict alias route cannot accept a supplied model route")
        return self._providers["ollama"].generate(request, context)

    def resolve_route(self, request: ModelRequest, context: OperationContext):
        """Delegate route identity to the same provider used for generation."""
        provider_name = self._provider_for_request(request)
        resolver = getattr(self._providers[provider_name], "resolve_route", None)
        if not callable(resolver):
            return None
        route = resolver(request, context)
        if route is None:
            return None
        if (
            not isinstance(route, ResolvedModelRoute)
            or route.provider_id != provider_name
            or route.tier != request.tier
        ):
            raise InvalidInput("provider returned a route for another binding")
        return replace(
            route, dispatch_provider=provider_name,
            _dispatch_issuer=self._route_issuer,
        )

    def generate(
        self, request: ModelRequest, context: OperationContext
    ) -> ModelResponse:
        provider = self._provider_for_request(request)
        if "_resolved_route" in (request.options or {}):
            raise InvalidInput("resolved routes cannot be supplied as model options")
        if request._resolved_route is not None:
            route = request._resolved_route
            if (
                not isinstance(route, ResolvedModelRoute)
                or route._dispatch_issuer is not self._route_issuer
                or route.dispatch_provider != provider
                or route.provider_id != provider
                or route.tier != request.tier
            ):
                raise InvalidInput("resolved model route was not issued by this dispatch")
        return self._providers[provider].generate(request, context)

    def embed(
        self, texts: Sequence[str], context: OperationContext
    ) -> Sequence[Embedding]:
        return self._providers[self._embedding_provider].embed(texts, context)

    def provider_status(self) -> Mapping[str, Mapping[str, object]]:
        """Aggregate each configured provider's content-free status.

        Providers that do not report status appear as ``unknown`` rather than
        being guessed healthy or unhealthy.
        """
        status: dict[str, Mapping[str, object]] = {}
        for name in sorted(self._providers):
            reporter = getattr(self._providers[name], "provider_status", None)
            if callable(reporter):
                reported = reporter()
                status.update({key: dict(value) for key, value in reported.items()})
                if name not in reported:
                    status[name] = {"provider": name, "state": "unknown"}
            else:
                status[name] = {"provider": name, "state": "unknown"}
        return status

    def capability_health(self, provider: str | None = None):
        """Delegate to one provider (the default generation provider if unset)."""
        name = provider or self._default_generation_provider
        if name not in self._providers:
            raise InvalidInput("provider %r is not configured" % name)
        reporter = getattr(self._providers[name], "capability_health", None)
        if not callable(reporter):
            raise InvalidInput("provider %r does not report capability health" % name)
        return reporter()
