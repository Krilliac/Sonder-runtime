"""Select exactly one configured ModelGateway for each request."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from ...application.context import OperationContext
from ...application.ports.model_gateway import (
    Embedding,
    ModelGateway,
    ModelRequest,
    ModelResponse,
)
from ...domain.common.errors import InvalidInput


class ProviderDispatchGateway:
    def __init__(
        self,
        *,
        providers: Mapping[str, ModelGateway],
        tier_providers: Mapping[str, str],
        embedding_provider: str,
    ) -> None:
        provider_map = dict(providers)
        tier_map = dict(tier_providers)
        required = set(tier_map.values()) | {embedding_provider}
        missing = sorted(required - set(provider_map))
        if missing:
            raise InvalidInput("missing provider gateways: %s" % ", ".join(missing))
        self._providers = MappingProxyType(provider_map)
        self._tier_providers = MappingProxyType(tier_map)
        self._embedding_provider = embedding_provider

    def generate(
        self, request: ModelRequest, context: OperationContext
    ) -> ModelResponse:
        provider = self._tier_providers.get(request.tier)
        if provider is None:
            raise InvalidInput("no provider binding for tier %r" % request.tier)
        return self._providers[provider].generate(request, context)

    def embed(
        self, texts: Sequence[str], context: OperationContext
    ) -> Sequence[Embedding]:
        return self._providers[self._embedding_provider].embed(texts, context)
