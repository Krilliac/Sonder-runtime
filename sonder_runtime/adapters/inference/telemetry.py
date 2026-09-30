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


def _prompt_cache_counts(usage: dict, timings: dict) -> tuple[int | None, int | None, int | None]:
    """``(prompt, cached, uncached)`` prompt tokens, each ``None`` when unknown.

    The total is ``usage.prompt_tokens`` only: ``timings.prompt_n`` means the
    whole prompt on Sonder Inference but only the uncached part on
    llama.cpp's own server, so it is never read as a total.  The cached
    subset is ``usage.prompt_tokens_details.cached_tokens``, else
    ``timings.cache_n``.  A missing or inconsistent value stays unknown; it
    never becomes a fabricated 0.
    """
    prompt = _count(usage.get("prompt_tokens"))
    details = usage.get("prompt_tokens_details")
    cached = _count(details.get("cached_tokens")) if isinstance(details, dict) else None
    if cached is None:
        cached = _count(timings.get("cache_n"))
    if cached is not None and prompt is not None and cached > prompt:
        cached = None
    uncached = prompt - cached if prompt is not None and cached is not None else None
    return prompt, cached, uncached


def _draft_counts(timings: dict) -> tuple[int | None, int | None]:
    drafted = _count(timings.get("draft_n"))
    accepted = _count(timings.get("draft_n_accepted"))
    if drafted is not None and accepted is not None and accepted > drafted:
        return None, None
    return drafted, accepted


def from_openai_compatible(payload: dict, *, with_usage: bool = False) -> InferenceTelemetry | None:
    """Normalize the bounded ``timings`` extension used by llama.cpp peers.

    Also reads ``timings.ttft_ms``, ``timings.cache_n`` and
    ``timings.draft_n``/``draft_n_accepted``.  ``with_usage`` (Sonder
    Inference) additionally reads the prompt total, cached prompt tokens and
    completion count from ``usage`` (see :func:`_prompt_cache_counts`);
    generic peers keep their historical timings-only telemetry.  Every absent
    field stays ``None`` (unknown).
    """
    timings = payload.get("timings")
    if not isinstance(timings, dict):
        timings = {}
    usage = payload.get("usage") if with_usage else None
    if not isinstance(usage, dict):
        usage = {}
    prompt_ms = _milliseconds(timings.get("prompt_ms"))
    eval_ms = _milliseconds(timings.get("predicted_ms"))
    prompt_tokens, cached_tokens, uncached_tokens = _prompt_cache_counts(usage, timings)
    drafted, accepted = _draft_counts(timings)
    output_tokens = _count(usage.get("completion_tokens"))
    if output_tokens is None and with_usage:
        output_tokens = _count(timings.get("predicted_n"))
    telemetry = InferenceTelemetry(
        prompt_tokens=prompt_tokens,
        prompt_cached_tokens=cached_tokens,
        prompt_uncached_tokens=uncached_tokens,
        output_tokens=output_tokens,
        ttft_ms=_milliseconds(timings.get("ttft_ms")),
        draft_tokens=drafted,
        draft_accepted_tokens=accepted,
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
