"""Provider-neutral model route and health facade.

The transport gateway remains an implementation detail of the adapter layer.
This facade owns the application-facing health publication and role route
contract, while capability routing and escalation remain injectable policy
services.  A newly composed provider is deliberately UNKNOWN until an
adapter or bootstrap health check publishes evidence.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..context import OperationContext
from ..ports.model_gateway import Embedding, ModelGateway, ModelRequest, ModelResponse
from ..routing.capability_router import CapabilityRouter, RouteDecision, RoutingRequest
from ...domain.common.errors import DependencyUnavailable
from .health_and_roles import (
    GatewayRoute,
    LogicalRole,
    ModelGatewayContract,
    ProviderHealth,
    RoleBinding,
)


class ModelGatewayFacade:
    """Application facade for provider health, role routes, and delegation.

    ``gateway`` is retained for compatibility with existing callers.  Calls
    through ``generate_for_role`` are health-gated; plain ``generate`` and
    ``embed`` preserve the existing typed transport port for services that
    already have their own route admission.
    """

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        providers: Mapping[str, object] | None = None,
        bindings: Mapping[LogicalRole, object] | None = None,
        health: Mapping[str, ProviderHealth] | None = None,
        capability_router: CapabilityRouter | None = None,
    ) -> None:
        self._gateway = gateway
        provider_map = dict(providers or {"local": gateway})
        route_bindings = dict(bindings or {
            LogicalRole.DEFAULT: RoleBinding(LogicalRole.DEFAULT, "local", "runtime")
        })
        self._contract = ModelGatewayContract(
            provider_map,
            route_bindings,
            health=health,
        )
        self._capability_router = capability_router

    @property
    def gateway(self) -> ModelGateway:
        return self._gateway

    def publish_health(self, snapshot: ProviderHealth) -> None:
        self._contract.publish_health(snapshot)

    def health(self, provider_id: str) -> ProviderHealth:
        return self._contract.health(provider_id)

    def health_snapshot(self) -> tuple[ProviderHealth, ...]:
        """Return all published snapshots in stable provider order."""
        return tuple(self._contract.health(provider_id) for provider_id in sorted(self._contract.providers))

    @property
    def route_health(self) -> tuple[ProviderHealth, ...]:
        """Compatibility spelling for health consumers at the route boundary."""
        return self.health_snapshot()

    def route(self, role: LogicalRole = LogicalRole.DEFAULT) -> GatewayRoute:
        return self._contract.route(role)

    def route_capabilities(self, request: RoutingRequest) -> RouteDecision:
        if self._capability_router is None:
            raise DependencyUnavailable("capability routing is not configured")
        return self._capability_router.route(request)

    def generate(self, request: ModelRequest, context: OperationContext) -> ModelResponse:
        return self._gateway.generate(request, context)

    def generate_for_role(
        self,
        request: ModelRequest,
        context: OperationContext,
        *,
        role: LogicalRole = LogicalRole.DEFAULT,
    ) -> ModelResponse:
        return self._contract.generate(request, context, role=role)

    def embed(self, texts: Sequence[str], context: OperationContext) -> Sequence[Embedding]:
        return self._gateway.embed(texts, context)


__all__ = ["ModelGatewayFacade"]
