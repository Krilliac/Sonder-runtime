"""WP3-SEAM-001 provider-neutral model generation contract.

This module is intentionally additive.  It describes the application-facing
provider boundary without importing or changing any existing gateway or
transport implementation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Iterable, Literal, Mapping, Protocol, Sequence

from ..context import OperationContext


Role = Literal["system", "user", "assistant"]
OptionValue = str | int | float | bool


@dataclass(frozen=True)
class GenerationMessage:
    """One ordered message supplied to a provider."""

    role: Role
    content: str

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("generation message content must not be empty")


@dataclass(frozen=True)
class GenerationRequest:
    """Complete input for one generation or streaming operation."""

    model: str
    messages: tuple[GenerationMessage, ...]
    options: Mapping[str, OptionValue] = field(default_factory=dict)
    request_id: str = ""

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("generation request model must not be empty")
        if not self.messages:
            raise ValueError("generation request requires at least one message")
        if any(not isinstance(message, GenerationMessage) for message in self.messages):
            raise TypeError("generation request messages must be GenerationMessage values")


@dataclass(frozen=True)
class GenerationResult:
    """Final provider result; usage fields are optional provider facts."""

    text: str
    model: str
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class GenerationChunk:
    """An ordered streaming delta.  The final chunk may carry finish data."""

    text: str = ""
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class Capability(StrEnum):
    GENERATION = "generation"
    STREAMING = "streaming"


@dataclass(frozen=True)
class CapabilityHealth:
    """Point-in-time, provider-reported health for contract capabilities."""

    provider: str
    capabilities: frozenset[Capability]
    healthy: bool
    checked_at: datetime
    detail: str = ""

    def supports(self, capability: Capability) -> bool:
        return self.healthy and capability in self.capabilities


class ModelGatewayProvider(Protocol):
    """Application port implemented by each model provider adapter."""

    # [async safe] Implementations own transport threads/resources.
    def generate(
        self, request: GenerationRequest, context: OperationContext
    ) -> GenerationResult: ...

    # [async safe] The iterable is single-consumer and preserves provider order.
    def stream(
        self, request: GenerationRequest, context: OperationContext
    ) -> Iterable[GenerationChunk]: ...

    # [any thread, thread-safe] Must not perform generation.
    def capability_health(self) -> CapabilityHealth: ...


__all__ = [
    "Capability",
    "CapabilityHealth",
    "GenerationChunk",
    "GenerationMessage",
    "GenerationRequest",
    "GenerationResult",
    "ModelGatewayProvider",
]
