"""Privacy-safe normalization of backend inference measurements."""
from __future__ import annotations

import math

from ...application.ports.model_gateway import InferenceTelemetry

_MAX_DURATION_MS = 86_400_000.0
_MAX_RATE = 1_000_000.0


def _number(value, *, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > maximum:
        return None
    return result


def _milliseconds(value) -> float | None:
    return _number(value, maximum=_MAX_DURATION_MS)


def _nanoseconds_to_ms(value) -> float | None:
    ns = _number(value, maximum=_MAX_DURATION_MS * 1_000_000.0)
    return None if ns is None else ns / 1_000_000.0


def _rate(value) -> float | None:
    return _number(value, maximum=_MAX_RATE)


def _count(value, *, maximum: int = 1_000_000_000) -> int | None:
    """Accept only finite, integral provider counts within a hard bound."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= maximum else None


def _derived_rate(count, duration_ms: float | None) -> float | None:
    measured_count = _number(count, maximum=1_000_000_000.0)
    if measured_count is None or duration_ms is None or duration_ms <= 0:
        return None
    return _rate(measured_count * 1000.0 / duration_ms)


def _reported_or_derived_rate(reported, count, duration_ms) -> float | None:
    measured = _rate(reported)
    return measured if measured is not None else _derived_rate(count, duration_ms)


def _load_state(payload: dict) -> str | None:
    state = payload.get("load_state")
    if isinstance(state, str) and state.casefold() in ("cold", "warm"):
        return state.casefold()
    cold_start = payload.get("cold_start")
    if isinstance(cold_start, bool):
        return "cold" if cold_start else "warm"
    return None


def from_ollama(payload: dict) -> InferenceTelemetry | None:
    """Normalize Ollama's documented nanosecond measurements."""
    prompt_tokens = _count(payload.get("prompt_eval_count"))
    cached_tokens = _count(payload.get("prompt_eval_cached_count"))
    # Ollama documents cached prompt tokens as a subset of prompt_eval_count.
    # Do not manufacture a cache result when either field is absent or invalid.
    if (
        prompt_tokens is None
        or cached_tokens is None
        or cached_tokens > prompt_tokens
    ):
        cached_tokens = None
        uncached_tokens = None
    else:
        uncached_tokens = prompt_tokens - cached_tokens
    total_ms = _nanoseconds_to_ms(payload.get("total_duration"))
    load_ms = _nanoseconds_to_ms(payload.get("load_duration"))
    prompt_ms = _nanoseconds_to_ms(payload.get("prompt_eval_duration"))
    eval_ms = _nanoseconds_to_ms(payload.get("eval_duration"))
    telemetry = InferenceTelemetry(
        backend_total_ms=total_ms,
        load_ms=load_ms,
        prompt_eval_ms=prompt_ms,
        eval_ms=eval_ms,
        prompt_tokens=prompt_tokens,
        prompt_cached_tokens=cached_tokens,
        prompt_uncached_tokens=uncached_tokens,
        output_tokens=_count(payload.get("eval_count")),
        prompt_tokens_per_second=_derived_rate(
            payload.get("prompt_eval_count"), prompt_ms
        ),
        output_tokens_per_second=_derived_rate(payload.get("eval_count"), eval_ms),
        load_state=_load_state(payload),
    )
    return telemetry if any(value is not None for value in telemetry.__dict__.values()) else None


def from_openai_compatible(payload: dict) -> InferenceTelemetry | None:
    """Normalize the bounded ``timings`` extension used by llama.cpp peers."""
    timings = payload.get("timings")
    if not isinstance(timings, dict):
        timings = {}
    prompt_ms = _milliseconds(timings.get("prompt_ms"))
    eval_ms = _milliseconds(timings.get("predicted_ms"))
    telemetry = InferenceTelemetry(
        backend_total_ms=_milliseconds(timings.get("total_ms")),
        load_ms=_milliseconds(timings.get("load_ms")),
        prompt_eval_ms=prompt_ms,
        eval_ms=eval_ms,
        prompt_tokens_per_second=_reported_or_derived_rate(
            timings.get("prompt_per_second"), timings.get("prompt_n"), prompt_ms
        ),
        output_tokens_per_second=_reported_or_derived_rate(
            timings.get("predicted_per_second"),
            timings.get("predicted_n"),
            eval_ms,
        ),
        load_state=_load_state(payload),
    )
    return telemetry if any(value is not None for value in telemetry.__dict__.values()) else None


__all__ = ["from_ollama", "from_openai_compatible"]
