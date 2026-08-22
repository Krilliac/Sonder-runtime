"""Measured, provider-neutral native context sizing policy (WP4 CTX-008).

The policy consumes a measurement made by an owning hardware/model adapter. It
does not discover hardware, call a provider, or mutate any existing context
policy. A measurement is a successful model/KV-cache operating point: the
context size and free memory observed at that point, followed by the current
free memory reading.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor, isfinite
from typing import Optional


DEFAULT_FALLBACK_CONTEXT_TOKENS = 8_192
DEFAULT_MIN_CONTEXT_TOKENS = 512
DEFAULT_MAX_CONTEXT_TOKENS = 262_144
DEFAULT_MEMORY_SAFETY_MARGIN = 0.80
DEFAULT_TOKEN_SAFETY_MARGIN = 0.90


@dataclass(frozen=True)
class MeasuredContextCapability:
    """One measured model/KV-cache capability point.

    ``measured_free_memory_gb`` is the free VRAM (or RAM for CPU execution)
    when ``measured_context_tokens`` succeeded. ``available_memory_gb`` is the
    current reading for the same model, quantization, and KV-cache type. The
    caller owns proving that those conditions match; this module only applies
    the arithmetic and conservative validation.
    """

    measured_context_tokens: int
    measured_free_memory_gb: float
    available_memory_gb: float
    model_id: str = ""
    kv_cache_type: str = ""


@dataclass(frozen=True)
class ContextSizing:
    """Immutable explanation of one native context sizing decision."""

    context_tokens: int
    source: str
    reason: str
    raw_context_tokens: Optional[int]
    memory_safety_margin: float
    token_safety_margin: float


def _finite_positive(value) -> bool:
    try:
        return isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _valid_measurement(capability) -> bool:
    if not isinstance(capability, MeasuredContextCapability):
        return False
    if isinstance(capability.measured_context_tokens, bool):
        return False
    if not isinstance(capability.measured_context_tokens, int):
        return False
    return (
        capability.measured_context_tokens > 0
        and _finite_positive(capability.measured_free_memory_gb)
        and _finite_positive(capability.available_memory_gb)
    )


def _margin(value, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError("%s must be between 0 and 1" % name)
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be between 0 and 1" % name) from exc
    if not isfinite(value) or not 0 < value <= 1:
        raise ValueError("%s must be between 0 and 1" % name)
    return value


def _bounds(minimum, maximum) -> tuple[int, int]:
    if (
        isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or minimum < 1
        or maximum < minimum
    ):
        raise ValueError("context bounds must be positive integers with maximum >= minimum")
    return minimum, maximum


def _bound(value, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("context bounds must be integers")
    return max(minimum, min(maximum, value))


def size_native_context(
    capability: Optional[MeasuredContextCapability],
    *,
    memory_safety_margin: float = DEFAULT_MEMORY_SAFETY_MARGIN,
    token_safety_margin: float = DEFAULT_TOKEN_SAFETY_MARGIN,
    fallback_context_tokens: int = DEFAULT_FALLBACK_CONTEXT_TOKENS,
    minimum_context_tokens: int = DEFAULT_MIN_CONTEXT_TOKENS,
    maximum_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
) -> ContextSizing:
    """Choose a safe native context size from a measured capability point.

    The measured operating point is reduced by both explicit safety margins
    and bounded by the caller's policy limits. A higher current-memory reading
    may scale the point upward; a lower reading never shrinks below the proven
    operating point. Rounding is always downward.
    Invalid, stale, or unavailable measurements take the same deterministic
    fallback path; no optimistic estimate is fabricated.
    """
    memory_margin = _margin(memory_safety_margin, "memory_safety_margin")
    token_margin = _margin(token_safety_margin, "token_safety_margin")
    minimum, maximum = _bounds(minimum_context_tokens, maximum_context_tokens)
    fallback = _bound(
        fallback_context_tokens, minimum=minimum, maximum=maximum
    )

    if not _valid_measurement(capability):
        return ContextSizing(
            fallback, "fallback", "invalid_or_unavailable_measurement", None,
            memory_margin, token_margin,
        )

    ratio = max(1.0, float(capability.available_memory_gb) / float(capability.measured_free_memory_gb))
    raw = floor(
        capability.measured_context_tokens
        * ratio
        * memory_margin
        * token_margin
    )
    sized = max(minimum, min(maximum, raw))
    return ContextSizing(
        sized, "measured", "scaled_measured_capability", raw,
        memory_margin, token_margin,
    )


native_context_size = size_native_context


__all__ = [
    "ContextSizing",
    "MeasuredContextCapability",
    "DEFAULT_FALLBACK_CONTEXT_TOKENS",
    "DEFAULT_MAX_CONTEXT_TOKENS",
    "DEFAULT_MEMORY_SAFETY_MARGIN",
    "DEFAULT_MIN_CONTEXT_TOKENS",
    "DEFAULT_TOKEN_SAFETY_MARGIN",
    "native_context_size",
    "size_native_context",
]
