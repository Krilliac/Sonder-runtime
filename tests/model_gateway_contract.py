"""Reusable offline contract harness for ModelGateway adapters.

Registering another provider requires one factory in
``test_model_gateway_conformance.py``.  The shared ladder deliberately stops at
the injected transport/driver seam; optional live smoke tests live separately
under ``tests/live``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sonder_runtime.application.context import OperationContext
from sonder_runtime.application.ports.model_gateway import Embedding, ModelResponse


@dataclass(frozen=True)
class ProviderCapabilities:
    """Explicitly supported optional portions of the common contract."""

    system_prompt: bool
    generation_options: bool
    usage_accounting: bool
    transport_deadline: bool
    cooperative_cancellation: bool
    context_consent: bool


@dataclass
class GatewayContractProbe:
    """A provider-neutral view over one fully offline adapter double."""

    name: str
    gateway: object
    capabilities: ProviderCapabilities
    generate: Callable[[OperationContext], ModelResponse]
    embed: Callable[[OperationContext], list[Embedding]]
    set_success: Callable[[object, object, object], None]
    set_usage_object: Callable[[object], None]
    set_embedding: Callable[[object], None]
    captured_request: Callable[[], dict]
    captured_embedding_input: Callable[[], tuple[str, ...]]
    call_count: Callable[[], int]
    transport_timeout: Callable[[], float | int | None]
    cooperative_cancel_hook: Callable[[], bool]


class CancelledToken:
    cancelled = True

    def wait(self, timeout=None):
        return True
