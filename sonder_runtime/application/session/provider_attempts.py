"""Context-local ownership for final provider attempts; never replay authority.

Scopes bind in the execution thread. A returned JSON response is transport
evidence, not an accepted answer; validation/repair and transcript ownership
remain with the caller. Missing terminal evidence means an unknown outcome.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import inspect
import math

from ...domain.common.errors import IntegrityFailure, InternalFailure, SonderError

_MAX_DURATION_MS = 86_400_000.0
_MAX_RATE = 1_000_000.0
_MAX_TOKEN_COUNT = 1_000_000_000


_owner = ContextVar("provider_attempt_owner", default=None)

# One process-wide, content-free observer of provider sends (installed by the
# composition root for live telemetry).  It sees the provider label, the
# operation, the model name and usage counts -- never a payload or response
# body -- and it can neither fail nor delay a send: every call into it is
# guarded, and it runs on the sending thread only for bookkeeping.
_attempt_observer = None


def install_provider_attempt_observer(observer) -> None:
    """Install the observer notified around every ``dispatch_provider`` send."""
    global _attempt_observer
    for name in ("provider_send_started", "provider_send_finished"):
        if not callable(getattr(observer, name, None)):
            raise TypeError("provider attempt observer must implement %s" % name)
    _attempt_observer = observer


def clear_provider_attempt_observer(observer=None) -> None:
    """Remove ``observer`` (or any observer when None); a stale owner is a no-op."""
    global _attempt_observer
    if observer is None or _attempt_observer is observer:
        _attempt_observer = None


def report_provider_fallback(from_provider, to_provider, reason_code) -> None:
    """Tell the observer a request moved to another provider before any send.

    A pre-send refusal (for example a cached not-ready health state) never
    reaches ``dispatch_provider``, so a fallback wrapper reports the change
    here.  Observers without ``provider_fallback`` ignore it; it never raises.
    """
    observer = _attempt_observer
    hook = getattr(observer, "provider_fallback", None)
    if not callable(hook):
        return
    try:
        hook(str(from_provider), str(to_provider), str(reason_code))
    except Exception:
        pass


def report_route_overflow(fields) -> None:
    """Tell the observer a turn moved to the long-context overflow model.

    ``fields`` are the content-free ``route.changed`` attributes the overflow
    decision builds.  Observers without ``route_overflow`` ignore it; it never
    raises.
    """
    hook = getattr(_attempt_observer, "route_overflow", None)
    if not callable(hook):
        return
    try:
        hook(dict(fields or {}))
    except Exception:
        pass


def report_execution_route(fields: Mapping[str, object] | None) -> None:
    """Tell the observer which content-free execution route was chosen.

    Observers without ``execution_route_decided`` ignore it; it never raises.
    """
    hook = getattr(_attempt_observer, "execution_route_decided", None)
    if not callable(hook):
        return
    try:
        hook(dict(fields or {}))
    except Exception:
        pass


def report_fanout_event(
    phase: str,
    fields: Mapping[str, object] | None = None,
    *,
    run_id: str | None = None,
) -> None:
    """Tell the observer a content-free ``fanout.<phase>`` lifecycle event.

    Observers without ``fanout_event`` ignore it; it never raises.
    """
    hook = getattr(_attempt_observer, "fanout_event", None)
    if not callable(hook):
        return
    try:
        hook(str(phase), dict(fields or {}), run_id=run_id)
    except Exception:
        pass


def report_ensemble_event(
    phase: str,
    fields: Mapping[str, object] | None = None,
    *,
    run_id: str | None = None,
) -> None:
    """Tell the observer a content-free ``ensemble.<phase>`` lifecycle event.

    Observers without ``ensemble_event`` ignore it; it never raises.
    """
    hook = getattr(_attempt_observer, "ensemble_event", None)
    if not callable(hook):
        return
    try:
        hook(str(phase), dict(fields or {}), run_id=run_id)
    except Exception:
        pass


def _count(value) -> int | None:
    return value if type(value) is int and 0 <= value <= _MAX_TOKEN_COUNT else None


def _finite_number(value, *, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0.0 or result > maximum:
        return None
    return result


def _ms(value) -> float | None:
    measured = _finite_number(value, maximum=_MAX_DURATION_MS)
    return None if measured is None else round(measured, 3)


def _ns_to_ms(value) -> float | None:
    ns = _finite_number(value, maximum=_MAX_DURATION_MS * 1_000_000.0)
    return None if ns is None else round(ns / 1_000_000.0, 3)


def _rate(value) -> float | None:
    measured = _finite_number(value, maximum=_MAX_RATE)
    return None if measured is None else round(measured, 3)


def _derived_rate(count, duration_ms: float | None) -> float | None:
    measured_count = _finite_number(count, maximum=float(_MAX_TOKEN_COUNT))
    if measured_count is None or duration_ms is None or duration_ms <= 0.0:
        return None
    return _rate(measured_count * 1000.0 / duration_ms)


def _accepts_keyword(fn, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        param.kind == inspect.Parameter.VAR_KEYWORD
        or (
            param.name == name
            and param.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        )
        for param in params
    )


def _response_evidence(result):
    """Extract (model, prompt_tokens, completion_tokens) from a provider reply.

    Both wire shapes are understood: Ollama's ``prompt_eval_count`` /
    ``eval_count`` and OpenAI-compatible ``usage``.  Text fields are never
    read.
    """
    if not isinstance(result, dict):
        return None, None, None
    model = result.get("model") if isinstance(result.get("model"), str) else None
    prompt, completion = _count(result.get("prompt_eval_count")), _count(result.get("eval_count"))
    usage = result.get("usage")
    if isinstance(usage, dict):
        if prompt is None:
            prompt = _count(usage.get("prompt_tokens"))
        if completion is None:
            completion = _count(usage.get("completion_tokens"))
    return model, prompt, completion


def _response_timings(
    result,
    *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> dict[str, object]:
    """Extract bounded, content-free inference timings from a provider reply.

    Understands both Ollama nanosecond durations/cache counts and
    OpenAI-compatible / Sonder-Inference ``timings`` + ``usage`` shapes,
    guarding ``prompt_eval_tps`` against KV-cache-hit inflation.  Returns an
    empty dict when the reply reported no timing or cache evidence.  Text
    fields are never read.
    """
    if not isinstance(result, dict):
        return {}
    raw_timings = result.get("timings")
    timings: dict[str, object] = dict(raw_timings) if isinstance(raw_timings, dict) else {}
    usage = result.get("usage")
    if isinstance(usage, dict):
        sonder_meta = usage.get("sonder")
        if isinstance(sonder_meta, dict) and isinstance(sonder_meta.get("timings"), dict):
            for key, value in sonder_meta["timings"].items():
                if key not in timings:
                    timings[key] = value
    else:
        usage = {}

    cached = _count(result.get("prompt_eval_cached_count"))
    if cached is None:
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = _count(details.get("cached_tokens"))
    if cached is None:
        cached = _count(timings.get("cache_n"))
    if cached is not None and prompt_tokens is not None and cached > prompt_tokens:
        cached = None
    uncached = (
        prompt_tokens - cached
        if prompt_tokens is not None and cached is not None
        else None
    )

    ttft_ms = _ms(timings.get("ttft_ms"))
    if ttft_ms is None:
        ttft_ms = _ms(result.get("ttft_ms"))
    prompt_eval_ms = _ms(timings.get("prompt_ms"))
    if prompt_eval_ms is None:
        prompt_eval_ms = _ms(timings.get("prompt_eval_ms"))
    if prompt_eval_ms is None:
        prompt_eval_ms = _ns_to_ms(result.get("prompt_eval_duration"))
    eval_ms = _ms(timings.get("predicted_ms"))
    if eval_ms is None:
        eval_ms = _ms(timings.get("eval_ms"))
    if eval_ms is None:
        eval_ms = _ns_to_ms(result.get("eval_duration"))
    load_ms = _ms(timings.get("load_ms"))
    if load_ms is None:
        load_ms = _ns_to_ms(result.get("load_duration"))
    queue_ms = _ms(timings.get("queue_ms"))
    if queue_ms is None:
        queue_ms = _ms(timings.get("queue_wait_ms"))
    if queue_ms is None:
        queue_ms = _ns_to_ms(result.get("queue_duration"))
    backend_total_ms = _ms(timings.get("total_ms"))
    if backend_total_ms is None:
        backend_total_ms = _ns_to_ms(result.get("total_duration"))

    # Guard prompt_eval_tps against cache-hit inflation: when cached tokens
    # are positive, derive strictly from uncached tokens / prompt_eval_ms.
    reported_prompt_tps = _rate(timings.get("prompt_per_second"))
    prompt_n = _count(timings.get("prompt_n"))
    prompt_eval_tps: float | None = None
    if cached is not None and cached > 0:
        if uncached is not None:
            if uncached > 0:
                prompt_eval_tps = _derived_rate(uncached, prompt_eval_ms)
        elif prompt_n is not None and prompt_n > 0:
            derived = _derived_rate(prompt_n, prompt_eval_ms)
            if derived is not None:
                if reported_prompt_tps is not None and reported_prompt_tps <= derived * 1.05:
                    prompt_eval_tps = reported_prompt_tps
                else:
                    prompt_eval_tps = derived
    elif not (uncached == 0 and cached == 0 and prompt_tokens == 0):
        prompt_eval_tps = (
            reported_prompt_tps
            if reported_prompt_tps is not None
            else _derived_rate(
                prompt_n if prompt_n is not None else prompt_tokens,
                prompt_eval_ms,
            )
        )

    reported_decode_tps = _rate(timings.get("predicted_per_second"))
    predicted_n = _count(timings.get("predicted_n"))
    decode_tps = (
        reported_decode_tps
        if reported_decode_tps is not None
        else _derived_rate(
            predicted_n if predicted_n is not None else completion_tokens,
            eval_ms,
        )
    )

    out: dict[str, object] = {}
    for key, value in (
        ("ttft_ms", ttft_ms),
        ("prompt_cached_tokens", cached),
        ("prompt_uncached_tokens", uncached),
        ("prompt_eval_ms", prompt_eval_ms),
        ("eval_ms", eval_ms),
        ("load_ms", load_ms),
        ("queue_ms", queue_ms),
        ("backend_total_ms", backend_total_ms),
        ("prompt_eval_tps", prompt_eval_tps),
        ("decode_tps", decode_tps),
    ):
        if value is not None:
            out[key] = value
    return out


# Legacy transport failures carry a ``kind`` (ModelCallError); telemetry
# exports the domain code instead of a Python class name.  The HTTP chat
# route maps a turn's terminal ModelCallError kind with the same table.
MODEL_ERROR_KIND_CODES = {
    "timeout": "DEADLINE_EXCEEDED",
    "cancelled": "CANCELLED",
    "configuration": "INVALID_INPUT",
    "unsupported_feature": "INVALID_INPUT",
    "provider_unavailable": "DEPENDENCY_UNAVAILABLE",
    "transport": "DEPENDENCY_UNAVAILABLE",
    "request": "DEPENDENCY_UNAVAILABLE",
    "protocol": "DEPENDENCY_UNAVAILABLE",
    "empty_response": "DEPENDENCY_UNAVAILABLE",
}


def telemetry_error_code(error: BaseException) -> str:
    """Classify a provider-send failure as a domain error code.

    Domain errors keep their own code.  Transport failures are classified by
    duck type (this layer imports no transport module): a timeout is
    ``DEADLINE_EXCEEDED``, a refused or reset connection and a provider 5xx
    are ``DEPENDENCY_UNAVAILABLE``, a 429 is ``CAPACITY_EXCEEDED`` and another
    4xx is ``INVALID_INPUT``.  An interrupt is ``CANCELLED``; anything else is
    ``INTERNAL_FAILURE``.
    """
    if isinstance(error, SonderError):
        return error.code
    kind = getattr(error, "kind", None)
    if isinstance(kind, str) and kind in MODEL_ERROR_KIND_CODES:
        status = getattr(error, "status", None)
        if type(status) is int and status == 429:
            return "CAPACITY_EXCEEDED"
        return MODEL_ERROR_KIND_CODES[kind]
    if not isinstance(error, Exception):
        return "CANCELLED"
    reason = getattr(error, "reason", None)
    if isinstance(error, TimeoutError) or isinstance(reason, TimeoutError):
        return "DEADLINE_EXCEEDED"
    status = getattr(error, "code", None)
    if type(status) is int and 400 <= status <= 599:
        if status == 429:
            return "CAPACITY_EXCEEDED"
        if status in (408, 504):
            return "DEADLINE_EXCEEDED"
        return "DEPENDENCY_UNAVAILABLE" if status >= 500 else "INVALID_INPUT"
    if isinstance(error, OSError):
        return "DEPENDENCY_UNAVAILABLE"
    return InternalFailure.code


def _observed(provider, operation, payload, send):
    observer = _attempt_observer
    if observer is None:
        return send()
    requested = payload.get("model") if isinstance(payload, dict) else None
    try:
        handle = observer.provider_send_started(
            provider, operation, requested if isinstance(requested, str) else None,
        )
    except Exception:
        return send()
    try:
        result = send()
    except BaseException as error:
        try:
            observer.provider_send_finished(handle, error_code=telemetry_error_code(error))
        except Exception:
            pass
        raise
    try:
        model, prompt_tokens, completion_tokens = _response_evidence(result)
        timings = _response_timings(
            result,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        if timings and _accepts_keyword(observer.provider_send_finished, "timings"):
            observer.provider_send_finished(
                handle, model=model, prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens, timings=timings,
            )
        else:
            observer.provider_send_finished(
                handle, model=model, prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
    except Exception:
        pass
    return result


class ProviderCaptureFailure(IntegrityFailure):
    """Evidence storage failed; this is not a model transport failure."""


@dataclass
class _Owner:
    capture: object
    pending: object
    failure: ProviderCaptureFailure | None = None
    admit: object = None
    completed: object = None

    @property
    def admission(self):
        return (self.capture, self.pending) if self.pending is not None else None

    def fail(self, message, cause):
        self.failure = ProviderCaptureFailure(message)
        raise self.failure from cause


@contextmanager
def provider_attempt_scope(capture, pending):
    """Bind an admitted logical request, or explicitly disable capture with None."""
    owner = _Owner(capture, pending) if capture is not None and pending is not None else None
    token = _owner.set(owner)
    try:
        yield owner
        # Legacy repair/fallback callbacks may swallow Exception. A damaged
        # evidence scope must neither run another attempt nor publish success.
        if owner is not None and owner.failure is not None:
            raise owner.failure
    finally:
        _owner.reset(token)


@contextmanager
def deferred_provider_request_scope(admit):
    """Bind an explicit owner's admission callback; cache hits do not invoke it.

    Yield a scope whose ``admission`` is None before dispatch, or the capture
    service and committed request afterwards for surface-owned completion.
    None preserves an existing enclosing owner (including explicit opt-outs).
    """
    if admit is None or _owner.get() is not None:
        yield _owner.get()
        return
    owner = _Owner(None, None, admit=admit)
    token = _owner.set(owner)
    try:
        yield owner
        if owner.failure is not None:
            raise owner.failure
    finally:
        _owner.reset(token)


def complete_scoped_provider_request(session_id, response):
    """Complete an admitted legacy turn once; None retains retrospective capture."""
    owner = _owner.get()
    if owner is None or owner.pending is None or owner.pending.session_id != str(session_id):
        return None
    if owner.failure is not None:
        raise owner.failure
    if owner.completed is None:
        try:
            owner.completed = owner.capture.complete_request(owner.pending, model_response=response)
        except Exception as error:
            owner.fail("could not persist logical response", error)
    return owner.completed


def dispatch_provider(provider, operation, payload, send):
    """Commit the effective JSON body before exactly one transport invocation.

    No headers or endpoint URL enter this API. Capture errors are intentionally
    outside transport exception types so existing retry policies cannot replay
    a request after an evidence write failed. BaseException preserves an
    unresolved admission instead of manufacturing a known provider failure.
    """
    owner = _owner.get()
    if owner is None:
        return _observed(provider, operation, payload, send)
    if owner.failure is not None:
        raise owner.failure
    if owner.pending is None:
        try:
            owner.capture, owner.pending = owner.admit()
        except Exception as error:
            owner.fail("could not persist logical admission", error)
    capture, pending = owner.capture, owner.pending
    try:
        attempt = capture.begin_provider_attempt(pending, provider=provider, operation=operation, payload=payload)
    except Exception as error:
        owner.fail("could not persist provider admission", error)
    try:
        result = _observed(provider, operation, payload, send)
    except Exception as error:
        code = telemetry_error_code(error)
        try:
            capture.finish_provider_attempt(pending, attempt, error_code=code)
        except Exception as capture_error:
            owner.fail("could not persist provider failure", capture_error)
        raise
    try:
        capture.finish_provider_attempt(pending, attempt, response=result)
    except Exception as error:
        owner.fail("could not persist provider response", error)
    return result
