"""Deterministic composition root (SPEC-3 R-M8).

Runtime variants are assembled here — never through import-time global
initialization. Importing this module creates no directories, opens no
databases, reads no mutable environment state, starts no threads, probes
no hardware, and contacts no services; construction happens inside
``build_application`` and services stay lazy until first use.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..adapters.legacy.services import (
    LegacyModelGateway,
    LegacyPolicyRepository,
    OperationsEventSink,
    SystemClock,
)
from ..application.ports.clock import Clock
from ..application.ports.event_sink import EventSink
from ..application.ports.model_gateway import ModelGateway
from ..application.runtime_policy.use_cases import RuntimePolicyService

PROFILES = ("workstation-local", "server-private")


@dataclass(frozen=True)
class Application:
    profile: str
    runtime_policy: RuntimePolicyService
    model_gateway: ModelGateway
    events: EventSink
    clock: Clock


def build_application(profile: str = "workstation-local") -> Application:
    """Assemble one application graph for the selected profile.

    Entry points call this exactly once. As SPEC-3 phases extract more
    bounded contexts, their services join this graph; until then the
    legacy adapters wrap the root modules.
    """
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected {PROFILES}")
    return Application(
        profile=profile,
        runtime_policy=RuntimePolicyService(LegacyPolicyRepository()),
        model_gateway=LegacyModelGateway(),
        events=OperationsEventSink(),
        clock=SystemClock(),
    )


_default: Application | None = None


def default_app() -> Application:
    """Process-wide default graph for compatibility shims."""
    global _default
    if _default is None:
        _default = build_application()
    return _default


def reset_for_tests() -> None:
    global _default
    _default = None
