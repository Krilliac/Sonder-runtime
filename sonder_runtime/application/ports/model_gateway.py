"""Model transport port (SPEC-3 section 4)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

from ..context import OperationContext


@dataclass(frozen=True)
class ModelRequest:
    prompt: str
    tier: str
    system: str = ""
    history: tuple = ()
    options: dict = field(default_factory=dict)
    stream: bool = False


@dataclass(frozen=True)
class InferenceTelemetry:
    """Optional backend-measured inference phases.

    Every value is absent unless the serving backend reported enough evidence
    to measure it.  In particular, rates are never estimated from text length
    and ``load_state`` is not inferred from an arbitrary duration threshold.
    """

    backend_total_ms: float | None = None
    load_ms: float | None = None
    prompt_eval_ms: float | None = None
    eval_ms: float | None = None
    prompt_tokens_per_second: float | None = None
    output_tokens_per_second: float | None = None
    load_state: str | None = None


@dataclass(frozen=True)
class ModelResponse:
    text: str
    model: str
    tier: str
    duration_ms: int = 0
    tokens_in: int | None = None
    tokens_out: int | None = None
    telemetry: InferenceTelemetry | None = None


@dataclass(frozen=True)
class Embedding:
    vector: tuple[float, ...]
    model: str


class ModelGateway(Protocol):
    """Every model call goes through here — retry, timeout, cancellation,
    and endpoint-consent classification live behind this port. Local
    retries stay bounded; remote-Ollama and hosted calls stay
    single-attempt; consent gates cannot be bypassed by another lane."""

    def generate(
        self, request: ModelRequest, context: OperationContext
    ) -> ModelResponse: ...

    def embed(
        self, texts: Sequence[str], context: OperationContext
    ) -> Sequence[Embedding]: ...
