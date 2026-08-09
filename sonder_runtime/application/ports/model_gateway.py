"""Model transport port (SPEC-3 section 4)."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import numbers
from typing import Protocol, Sequence

from ..context import OperationContext
from ...domain.common.errors import DependencyUnavailable


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


def require_model_text(value: object) -> str:
    """Validate provider output before it crosses the gateway boundary."""
    if not isinstance(value, str) or not value.strip():
        raise DependencyUnavailable("model provider returned no usable text")
    return value


def optional_token_count(value: object, field_name: str) -> int | None:
    """Return a trustworthy optional usage count, rejecting coercion.

    Provider usage is accounting data.  Accepting strings, booleans, or
    negative values would make a malformed response look authoritative.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DependencyUnavailable(
            "model provider returned invalid %s" % field_name
        )
    return value


def require_embedding_vector(value: object) -> tuple[float, ...]:
    """Normalize a non-empty, finite numeric embedding vector."""
    if isinstance(value, (str, bytes)):
        raise DependencyUnavailable("model provider returned an invalid embedding")
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise DependencyUnavailable(
            "model provider returned an invalid embedding"
        ) from exc
    if not items:
        raise DependencyUnavailable("model provider returned an empty embedding")
    if any(
        isinstance(item, bool)
        or not isinstance(item, numbers.Real)
        or not math.isfinite(float(item))
        for item in items
    ):
        raise DependencyUnavailable("model provider returned an invalid embedding")
    return tuple(float(item) for item in items)


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
