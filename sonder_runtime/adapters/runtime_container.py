"""Canonical adapter for assembling the explicit SPEC-5 runtime graph."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..adapters.runtime_configuration import RuntimeConfig
from ..adapters.runtime_capabilities import RuntimeCapabilities
from ..application.agent_registry.unified import UnifiedAgentRegistryService
from ..adapters.persistence.fleet_registry import FleetStoreRegistryAdapter
from ..application.ports.clock import Clock
from ..application.ports.event_sink import EventSink
from ..application.ports.model_gateway import ModelGateway
from ..application.context_integration import ContextPlanningFacade
from ..application.model_gateway import ModelGatewayFacade


@dataclass(frozen=True)
class Runtime:
    """The assembled runtime graph. Every service is reachable from here."""

    config: RuntimeConfig
    capabilities: RuntimeCapabilities
    model_gateway: ModelGateway
    model_routes: ModelGatewayFacade
    events: EventSink
    clock: Clock
    # Fleet persistence and its owner lease are deliberately lazy.  The
    # packaged runtime can therefore be composed for health/configuration
    # commands without opening the fleet store or importing the legacy root
    # orchestrator module.
    agent_registry: Callable[[], UnifiedAgentRegistryService]
    context_planning: ContextPlanningFacade | None = None

    @property
    def model_gateway_facade(self) -> ModelGatewayFacade:
        """Provider-neutral route/health view of the transport gateway."""
        return self.model_routes


def build_runtime(
    config: RuntimeConfig,
    capabilities: RuntimeCapabilities,
) -> Runtime:
    """Assemble the explicit runtime graph without hidden global state."""
    from .logging_event_sink import LoggingEventSink
    from .system_clock import SystemClock
    from .local_observability import LocalObservabilitySink

    from .inference.model_gateway_factory import build_model_gateway

    gateway: ModelGateway = build_model_gateway(backend=config.model_backend)
    model_routes = ModelGatewayFacade(gateway)

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
        model_gateway=gateway,
        model_routes=model_routes,
        events=LocalObservabilitySink(LoggingEventSink()),
        clock=SystemClock(),
        agent_registry=get_agent_registry,
        context_planning=ContextPlanningFacade(),
    )
