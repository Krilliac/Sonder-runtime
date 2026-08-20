"""SPEC-5 composition root — the single authoritative construction path.

Constructs the full runtime graph from validated config and frozen
capabilities.  No default_app() singleton.  No lazy global database owner.
No hidden environment-based policy selection inside business modules.

During the SPEC-5 migration, this coexists with the legacy ``app.py``
builder.  New code uses ``build_runtime()``; legacy call sites use
``app.build_application()`` until their WP slice migrates them.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..adapters.runtime_capabilities import RuntimeCapabilities
from ..application.ports.model_gateway import ModelGateway
from ..application.ports.clock import Clock
from ..application.ports.event_sink import EventSink


@dataclass(frozen=True)
class RuntimeConfig:
    """Validated, frozen configuration — no environment reads after this."""
    profile: str = "workstation-local"
    model_backend: str = "ollama"
    sonder_home: str = ""


@dataclass(frozen=True)
class Runtime:
    """The assembled runtime graph.  Every service is reachable from here."""
    config: RuntimeConfig
    capabilities: RuntimeCapabilities
    model_gateway: ModelGateway
    events: EventSink
    clock: Clock


def build_runtime(
    config: RuntimeConfig,
    capabilities: RuntimeCapabilities,
) -> Runtime:
    """Assemble the full SPEC-5 runtime graph.

    Placeholder during WP1 — will grow as each WP slice adds its domain
    services.  The important contract is: one call, explicit deps, no globals.
    """
    from ..adapters.logging_event_sink import LoggingEventSink
    from ..adapters.system_clock import SystemClock
    from ..adapters.local_observability import LocalObservabilitySink

    if config.model_backend in ("openai", "openai-compatible", "llamacpp", "vllm"):
        from ..adapters.inference.openai_compat import OpenAICompatibleGateway
        gateway: ModelGateway = OpenAICompatibleGateway()
    else:
        from ..adapters.inference.ollama import OllamaGateway
        gateway = OllamaGateway()

    return Runtime(
        config=config,
        capabilities=capabilities,
        model_gateway=gateway,
        events=LocalObservabilitySink(LoggingEventSink()),
        clock=SystemClock(),
    )
