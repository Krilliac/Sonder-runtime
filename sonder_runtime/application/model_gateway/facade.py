"""Provider-neutral model route and health facade.

The transport gateway remains an implementation detail of the adapter layer.
This facade owns the application-facing health publication and role route
contract, while capability routing and escalation remain injectable policy
services.  A newly composed provider is deliberately UNKNOWN until an
adapter or bootstrap health check publishes evidence.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from ...domain.common.errors import DependencyUnavailable
from ...domain.routing.backend_conformance import (
    BackendCapability,
    BackendIdentity,
    EvidenceState,
)
from ..context import OperationContext
from ..ports.model_gateway import Embedding, ModelGateway, ModelRequest, ModelResponse
from ..routing.capability_router import CapabilityRouter, RouteDecision, RoutingRequest
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
        recent_evidence=None,
        identity_for: Callable[[GatewayRoute], BackendIdentity | None] | None = None,
        evidence_clock: Callable[[], float] | None = None,
    ) -> None:
        if (recent_evidence is None) != (identity_for is None):
            raise ValueError("host identity and recent evidence must be configured together")
        self._gateway = gateway
        provider_map = dict(providers or {"local": gateway})
        if recent_evidence is not None and (
            len(provider_map) != 1 or next(iter(provider_map.values())) is not gateway
        ):
            raise ValueError("identity-bound routing requires the same concrete provider")
        route_bindings = dict(bindings or {
            LogicalRole.DEFAULT: RoleBinding(LogicalRole.DEFAULT, "local", "runtime")
        })
        self._contract = ModelGatewayContract(
            provider_map,
            route_bindings,
            health=health,
        )
        self._capability_router = capability_router
        self._recent_evidence = recent_evidence
        self._identity_for = identity_for
        self._evidence_clock = evidence_clock

    @property
    def gateway(self) -> ModelGateway:
        # Once opted in, exposing the raw transport would bypass admission.
        return self if self._recent_evidence is not None else self._gateway

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
        return self._admit_route(role)[0]

    def _admit_route(self, role: LogicalRole) -> tuple[GatewayRoute, BackendIdentity | None]:
        route = self._contract.route(role)
        identity = None
        if self._recent_evidence is not None:
            requirements = {
                LogicalRole.DEFAULT: frozenset(),
                LogicalRole.EXPLORER: frozenset({BackendCapability.SUMMARIZATION}),
                LogicalRole.ARCHITECT: frozenset({BackendCapability.REASONING}),
                LogicalRole.EDITOR: frozenset({BackendCapability.CODING}),
                LogicalRole.VERIFIER: frozenset({BackendCapability.STRUCTURED}),
                LogicalRole.REVIEWER: frozenset({BackendCapability.CRITIC}),
                LogicalRole.INTEGRATOR: frozenset({
                    BackendCapability.TOOL_SEQUENTIAL, BackendCapability.TOOL_CONTINUATION,
                }),
            }[route.role]
            identity = self._identity_for(route)
            options = {} if self._evidence_clock is None else {"now": self._evidence_clock()}
            verdict = self._recent_evidence.assess(
                route.model, requirements, backend=route.provider_id,
                identity=identity,
                any_of=((frozenset({
                    BackendCapability.TOOL_NATIVE, BackendCapability.TOOL_FALLBACK,
                }),) if route.role is LogicalRole.INTEGRATOR else ()),
                **options,
            )
            if verdict.state is not EvidenceState.PASSED:
                raise DependencyUnavailable(
                    f"model route {route.role.value} refused: {verdict.reason_code}"
                )
        return route, identity

    def _require_current_identity(
        self, role: LogicalRole, route: GatewayRoute, identity: BackendIdentity | None,
    ) -> None:
        current, observed = self._admit_route(role)
        if (current.provider_id != route.provider_id or current.model != route.model
                or observed != identity):
            raise DependencyUnavailable("backend identity changed during model dispatch")

    def route_capabilities(self, request: RoutingRequest) -> RouteDecision:
        if self._capability_router is None:
            raise DependencyUnavailable("capability routing is not configured")
        return self._capability_router.route(request)

    def _require_request_route(
        self, request: ModelRequest, role: LogicalRole,
    ) -> tuple[GatewayRoute, BackendIdentity | None]:
        route, identity = self._admit_route(role)
        if (request.tier != route.model
                or request.options.get("model", route.model) != route.model):
            raise DependencyUnavailable("requested model differs from evidenced role route")
        return route, identity

    def generate(self, request: ModelRequest, context: OperationContext) -> ModelResponse:
        if self._recent_evidence is None:
            return self._gateway.generate(request, context)
        route, identity = self._require_request_route(request, LogicalRole.DEFAULT)
        self._require_current_identity(LogicalRole.DEFAULT, route, identity)
        response = self._gateway.generate(request, context)
        self._require_current_identity(LogicalRole.DEFAULT, route, identity)
        if response.model != route.model:
            raise DependencyUnavailable("backend answered with a different model")
        return response

    def generate_for_role(
        self,
        request: ModelRequest,
        context: OperationContext,
        *,
        role: LogicalRole = LogicalRole.DEFAULT,
    ) -> ModelResponse:
        if self._recent_evidence is not None:
            route, identity = self._require_request_route(request, role)
            self._require_current_identity(role, route, identity)
            response = self._contract.generate(request, context, role=role)
            self._require_current_identity(role, route, identity)
            if response.model != route.model:
                raise DependencyUnavailable("backend answered with a different model")
            return response
        return self._contract.generate(request, context, role=role)

    def embed(self, texts: Sequence[str], context: OperationContext) -> Sequence[Embedding]:
        if self._recent_evidence is None:
            return self._gateway.embed(texts, context)
        route, identity = self._admit_route(LogicalRole.DEFAULT)
        options = {} if self._evidence_clock is None else {"now": self._evidence_clock()}
        verdict = self._recent_evidence.assess(
            route.model, frozenset({BackendCapability.EMBEDDING}),
            backend=route.provider_id, identity=identity, **options,
        )
        if verdict.state is not EvidenceState.PASSED:
            raise DependencyUnavailable(f"embedding route refused: {verdict.reason_code}")
        self._require_current_identity(LogicalRole.DEFAULT, route, identity)
        result = self._gateway.embed(texts, context)
        self._require_current_identity(LogicalRole.DEFAULT, route, identity)
        if any(item.model != route.model for item in result):
            raise DependencyUnavailable("backend embedded with a different model")
        return result


__all__ = ["ModelGatewayFacade"]
