"""Canonical adapter for assembling the explicit SPEC-5 runtime graph."""
from __future__ import annotations

from dataclasses import dataclass

from ..adapters.runtime_configuration import RuntimeConfig
from ..adapters.runtime_capabilities import RuntimeCapabilities
from ..application.ports.clock import Clock
from ..application.ports.event_sink import EventSink
from ..application.ports.model_gateway import ModelGateway


@dataclass(frozen=True)
class Runtime:
    """The assembled runtime graph. Every service is reachable from here."""

    config: RuntimeConfig
    capabilities: RuntimeCapabilities
    model_gateway: ModelGateway
    events: EventSink
    clock: Clock


def build_runtime(
    config: RuntimeConfig,
    capabilities: RuntimeCapabilities,
) -> Runtime:
    """Assemble the explicit runtime graph without hidden global state."""
    from .logging_event_sink import LoggingEventSink
    from .system_clock import SystemClock
    from .local_observability import LocalObservabilitySink

    if config.model_backend in ("openai", "openai-compatible", "llamacpp", "vllm"):
        from .inference.openai_compat import OpenAICompatibleGateway

        gateway: ModelGateway = OpenAICompatibleGateway()
    else:
        from .inference.ollama import OllamaGateway

        gateway = OllamaGateway()

    return Runtime(
        config=config,
        capabilities=capabilities,
        model_gateway=gateway,
        events=LocalObservabilitySink(LoggingEventSink()),
        clock=SystemClock(),
    )
