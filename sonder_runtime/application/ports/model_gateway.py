"""Model transport port (SPEC-3 section 4)."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import numbers
from collections.abc import Mapping
from typing import Protocol, Sequence

from ..context import OperationContext
from ...domain.common.errors import DependencyUnavailable
from ..security.prompt_provenance import (
    ContextPacket,
    ModelRequestProvenance,
    PromptProvenanceBoundary,
    ProvenanceError,
)


def _evidence_field(value: object, name: str) -> object:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


@dataclass(frozen=True)
class ModelRequest:
    prompt: str
    tier: str
    system: str = ""
    history: tuple = ()
    options: dict = field(default_factory=dict)
    stream: bool = False
    provenance: ModelRequestProvenance | None = None
    context_packet: ContextPacket | None = None
    # Immutable context evidence produced by the live request builder.  These
    # fields are provider-boundary metadata, not model-visible prompt text.
    prefix_manifest: object | None = None
    replay_manifest: object | None = None
    prefix_cache_observation: object | None = None
    # In-process capability only. It is intentionally excluded from the
    # JSON-serializable durable request options captured for replay.
    _resolved_route: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Provider-bound cache evidence must describe this request's own
        # prefix. Replay reconstructs these fields as dictionaries, while live
        # requests carry immutable manifest values.
        if self.prefix_cache_observation is not None:
            if self.prefix_manifest is None or any(
                not isinstance(_evidence_field(self.prefix_manifest, field), str)
                or not _evidence_field(self.prefix_manifest, field)
                or _evidence_field(self.prefix_manifest, field)
                != _evidence_field(self.prefix_cache_observation, field)
                for field in ("cache_key", "identity_key", "version")
            ):
                raise ValueError("cache observation does not match the request prefix")
        if self.prefix_manifest is not None and self.replay_manifest is not None:
            if _evidence_field(self.replay_manifest, "prefix_key") != _evidence_field(
                self.prefix_manifest, "cache_key"
            ):
                raise ValueError("replay manifest does not match the request prefix")
        # Ordinary user-authored prompts may remain unlabelled.  Any request
        # carrying prompt-visible external material must carry both halves of
        # the binding; partial metadata is never treated as trustworthy.
        if (self.provenance is None) != (self.context_packet is None):
            raise ProvenanceError(
                "model requests require both provenance and context_packet"
            )
        if self.provenance is not None and self.context_packet is not None:
            PromptProvenanceBoundary().validate_model_request(
                self.prompt,
                system=self.system,
                history=self.history,
                context=self.context_packet,
                binding=self.provenance,
            )


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
    prompt_tokens: int | None = None
    prompt_cached_tokens: int | None = None
    prompt_uncached_tokens: int | None = None
    output_tokens: int | None = None
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
