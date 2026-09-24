"""Canonical adapter for assembling the explicit SPEC-5 runtime graph."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..adapters.persistence.fleet_registry import FleetStoreRegistryAdapter
from ..adapters.runtime_capabilities import RuntimeCapabilities
from ..adapters.runtime_configuration import RuntimeConfig
from ..application.agent_registry.unified import UnifiedAgentRegistryService
from ..application.context_integration import ContextPlanningFacade
from ..application.execution.facade import ExecutionApplicationFacade
from ..application.model_gateway import ModelGatewayFacade
from ..application.model_gateway.health_and_roles import (
    GatewayRoute,
    LogicalRole,
    ProviderHealth,
    RoleBinding,
)
from ..application.ports.clock import Clock
from ..application.ports.event_sink import EventSink
from ..application.ports.model_gateway import ModelGateway
from ..application.ports.tool_registry import InMemoryToolRegistry
from ..application.protocol.facade import ProtocolApplicationFacade
from ..application.routing.backend_conformance import RecentCapabilityEvidence
from ..application.tools.facade import ToolApplicationFacade
from ..domain.routing.backend_conformance import BackendIdentity
from .provider_bindings import ProviderBindings, normalize_provider


@dataclass(frozen=True)
class Runtime:
    """The assembled runtime graph. Every service is reachable from here."""

    config: RuntimeConfig
    capabilities: RuntimeCapabilities
    model_gateway: ModelGateway
    provider_bindings: ProviderBindings
    model_routes: ModelGatewayFacade
    events: EventSink
    clock: Clock
    # Fleet persistence and its owner lease are deliberately lazy.  The
    # packaged runtime can therefore be composed for health/configuration
    # commands without opening the fleet store or importing the legacy root
    # orchestrator module.
    agent_registry: Callable[[], UnifiedAgentRegistryService]
    context_planning: ContextPlanningFacade | None = None
    execution: ExecutionApplicationFacade | None = None
    tools: ToolApplicationFacade | None = None
    protocol: ProtocolApplicationFacade | None = None

    @property
    def model_gateway_facade(self) -> ModelGatewayFacade:
        """Provider-neutral route/health view of the transport gateway."""
        return self.model_routes


def build_runtime(
    config: RuntimeConfig,
    capabilities: RuntimeCapabilities,
    *,
    route_evidence: RecentCapabilityEvidence | None = None,
    route_identity_for: Callable[[GatewayRoute], BackendIdentity | None] | None = None,
    route_bindings: Mapping[LogicalRole, RoleBinding] | None = None,
    route_health: Mapping[str, ProviderHealth] | None = None,
) -> Runtime:
    """Assemble the graph; host opt-in gates every public model gateway call."""
    if route_evidence is None:
        if route_identity_for is not None or route_bindings is not None or route_health is not None:
            raise ValueError("identity-bound routing requires all host-owned route inputs")
    elif route_identity_for is None or not route_bindings:
        raise ValueError("identity-bound routing requires current identity and explicit bindings")
    from .inference.model_gateway_factory import build_model_gateway
    from .local_observability import LocalObservabilitySink
    from .logging_event_sink import LoggingEventSink
    from .system_clock import SystemClock
    bindings = config.provider_bindings or ProviderBindings.uniform(config.model_backend)
    if route_evidence is not None:
        if len(bindings.required_providers) != 1:
            raise ValueError("identity-bound runtime requires a single concrete provider")
        selected = next(iter(bindings.required_providers))
        try:
            matched = all(normalize_provider(binding.provider_id) == selected
                          for binding in route_bindings.values())
        except ValueError:
            matched = False
        if not matched:
            raise ValueError("identity-bound route differs from the configured provider")
    gateway: ModelGateway = build_model_gateway(bindings)
    if route_evidence is None:
        model_routes = ModelGatewayFacade(gateway)
    else:
        provider_ids = {binding.provider_id for binding in route_bindings.values()}
        if len(provider_ids) != 1:
            raise ValueError("identity-bound runtime requires exactly one concrete provider binding")
        provider_id = provider_ids.pop()
        model_routes = ModelGatewayFacade(
            gateway, providers={provider_id: gateway}, bindings=route_bindings,
            health=route_health, recent_evidence=route_evidence,
            identity_for=route_identity_for,
        )
    # This graph is intentionally inert until a host supplies provider
    # adapters.  Its policy and executor defaults remain fail-closed.
    execution = ExecutionApplicationFacade.local()
    # Redaction is the one gateway default that is honest-but-open
    # (IdentityRedactor). The composition root can read the environment, so
    # it injects the real authority: platform value-based scrubbing (live
    # secret env values) composed with the canonical domain pattern set.
    from ..application.tools.facade import PatternOutputRedactor
    from ..platform.logging import Redactor

    tools = ToolApplicationFacade.compose(
        InMemoryToolRegistry(),
        redactor=PatternOutputRedactor(Redactor().redact),
    )
    # Derive the portable client/SDK schema from the same tool catalog.  No
    # live streams are invented here: hosts add authorized stream instances
    # through the protocol facade when they own a reconnectable session.
    protocol = ProtocolApplicationFacade.compose(tools.catalogs)

    agent_registry: UnifiedAgentRegistryService | None = None

    def get_agent_registry() -> UnifiedAgentRegistryService:
        nonlocal agent_registry
        if agent_registry is None:
            agent_registry = UnifiedAgentRegistryService(FleetStoreRegistryAdapter())
            agent_registry.register_workbench_modes()
        return agent_registry

    return Runtime(
        config=config,
        capabilities=capabilities,
        model_gateway=(model_routes if route_evidence is not None else gateway),
        provider_bindings=bindings,
        model_routes=model_routes,
        events=LocalObservabilitySink(LoggingEventSink()),
        clock=SystemClock(),
        agent_registry=get_agent_registry,
        context_planning=ContextPlanningFacade(),
        execution=execution,
        tools=tools,
        protocol=protocol,
    )
