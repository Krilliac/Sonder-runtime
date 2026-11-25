"""Runtime live-telemetry vocabulary v1 (docs/architecture/observatory-telemetry.md).

This module owns the content-free Runtime event vocabulary exported to
Observatory: ``session.*``, ``request.*`` and ``route.*``.  It holds per-turn
state, numbers provider attempts inside a turn, and is installed as the
``dispatch_provider`` observer so every provider send inside a turn becomes a
``route.selected`` event.

What never leaves this module: prompts, responses, summaries, provider
payloads, headers or URLs.  Attributes are identifiers, bounded model labels,
counts and durations only.  Emission never raises into the caller and never
blocks on I/O; the sink behind it is a bounded in-memory ring.
"""
from __future__ import annotations

import math
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterator, Mapping

from ..ports.telemetry_sink import TelemetryEvent, TelemetrySink

RUNTIME_EVENT_VOCABULARY = "sonder.runtime.events"
RUNTIME_EVENT_VOCABULARY_MAJOR = 1

# dispatch_provider labels are persisted as capture evidence and must not
# change; the exported provider ids are the binding names.
PROVIDER_IDS: Mapping[str, str] = {
    "ollama": "ollama",
    "openai-compatible": "openai_compatible",
    "sonder-inference": "sonder_inference",
    "openrouter": "openrouter",
    "anthropic": "anthropic",
}

# The same mapping the sonder_inference gateway sends as X-Sonder-Workload.
WORKLOAD_BY_SOURCE: Mapping[str, str] = {
    "http": "interactive_user",
    "repl": "interactive_user",
    "mcp": "owner_orchestrator",
    "worker": "implementation_worker",
    "system": "maintenance",
}

SURFACES = frozenset({"http.chat_completions", "a2a"})
OUTCOMES = {"completed": "request.completed", "failed": "request.failed",
            "cancelled": "request.cancelled"}
EXECUTION_MODES = frozenset({"single", "ensemble", "fanout", "agent"})
DIFFICULTY_BANDS = frozenset({"quick", "standard", "hard", "multi_scope"})
ENSEMBLE_MODES = frozenset({"prose", "code"})
FANOUT_PHASES = frozenset({"started", "result", "completed", "failed"})
ENSEMBLE_PHASES = frozenset({"started", "completed", "failed"})

_CORRELATION_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_LABEL = re.compile(r"[A-Za-z0-9._:/@+-]{1,96}")
_SIGNAL_LABEL = re.compile(r"[a-z0-9_:-]{1,64}")
_REASON_CODE = re.compile(r"[a-z0-9_.:-]{1,64}")
_SCOPE_LABEL = re.compile(r"[a-z0-9_-]{1,32}")
_UNSAFE_LABEL = "[unsafe-label]"
_MAX_DURATION_MS = 86_400_000.0
_MAX_RATE = 1_000_000.0
_MAX_TOKEN_COUNT = 1_000_000_000
_MAX_LIST_ITEMS = 16

# Fixed domain reasons from execution_mode_router mapped to compact safe codes.
# Free-text reasons outside this map (and not matching _REASON_CODE) are dropped
# so prompt text can never ride out on a reason field.
_KNOWN_EXECUTION_REASONS: Mapping[str, str] = {
    "structured response_format requires single-model execution": "structured_output_single",
    "multimodal image input routes to single vision model": "multimodal_image_single",
    "caller pinned an explicit model/tier": "explicit_model_pin",
    "explicit single execution mode requested": "explicit_single",
    "auto execution-mode router disabled": "auto_router_disabled",
    "explicit ensemble execution mode requested": "explicit_ensemble",
    "ensemble unavailable: developer authorization required": "ensemble_requires_developer_auth",
    "ensemble degraded to single: fewer than 2 distinct local models": "ensemble_degraded_insufficient_models",
    "explicit fanout execution mode requested": "explicit_fanout",
    "fanout unavailable: developer authorization required": "fanout_requires_developer_auth",
    "fanout degraded to single: fewer than 2 distinct local models": "fanout_degraded_insufficient_models",
    "explicit agent execution mode requested": "explicit_agent",
    "agent mode unavailable: developer authorization required": "agent_requires_developer_auth",
    "quick low-complexity prompt routed to fast single tier": "quick_auto_single",
    "standard single-scope prompt routed to primary local tier": "standard_auto_single",
    "hard reasoning prompt auto-routed to parallel multi-model ensemble": "hard_auto_ensemble",
    "hard prompt degraded to single strongest tier: auto-ensemble unavailable": "hard_degraded_single",
    "hard prompt routed to single strongest tier": "hard_single",
    "multi-scope implementation/verification task auto-routed to /agent": "multi_scope_auto_agent",
    "multi-scope task stepped down to ensemble (auto-agent disabled)": "multi_scope_stepdown_ensemble",
    "multi-scope task kept on single tier (auto-agent disabled)": "multi_scope_stepdown_single",
    "multi-scope comparative/audit prompt auto-routed to bounded local fanout": "multi_scope_auto_fanout",
    "multi-scope prompt auto-routed to parallel multi-model ensemble": "multi_scope_auto_ensemble",
    "multi-scope prompt degraded to single strongest tier": "multi_scope_degraded_single",
}


def sanitize_correlation_id(value: object) -> str | None:
    """Return ``value`` when it is a cross-producer correlation id, else None.

    The grammar is the one Sonder-Inference accepts for X-Sonder-* headers, so
    an id that passes here survives the hop to the next producer unchanged.
    """
    if isinstance(value, str) and _CORRELATION_ID.fullmatch(value):
        return value
    return None


def turn_id(message_id: object, http_correlation: object) -> str:
    """The one turn id R used for telemetry and ``context.correlation_id``.

    An A2A messageId is used when it already satisfies the correlation
    grammar; otherwise the HTTP correlation id stands in, so a messageId
    outside ``[A-Za-z0-9._:-]{1,128}`` can never silently break the join.
    """
    for candidate in (message_id, http_correlation):
        clean = sanitize_correlation_id(candidate)
        if clean is not None:
            return clean
    return "turn-" + uuid.uuid4().hex


def provider_id(label: object) -> str:
    """Map a dispatch_provider label to its exported provider id."""
    text = str(label or "").strip().lower()
    return PROVIDER_IDS.get(text, text.replace("-", "_") or "unknown")


def bounded_label(value: object) -> str | None:
    """A model or selector label, at most 96 identifier characters.

    Caller-supplied selectors are free text on the wire.  Anything that does
    not look like a model label is replaced so free text cannot ride out on a
    label field.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > 96:
        text = text[:96]
    return text if _LABEL.fullmatch(text) else _UNSAFE_LABEL


def _non_negative_int(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= _MAX_TOKEN_COUNT else None


def _positive_int(value: object, *, maximum: int = 1_000_000) -> int | None:
    return value if type(value) is int and 1 <= value <= maximum else None


def _bounded_float(
    value: object,
    *,
    minimum: float = 0.0,
    maximum: float = _MAX_RATE,
    ndigits: int = 3,
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < minimum or numeric > maximum:
        return None
    return round(numeric, ndigits)


def _bounded_label_list(values: object, *, limit: int = _MAX_LIST_ITEMS) -> list[str]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        label = bounded_label(item)
        if label is None or label == _UNSAFE_LABEL or label in seen:
            continue
        seen.add(label)
        result.append(label)
        if len(result) >= limit:
            break
    return result


def _bounded_signal_list(values: object, *, limit: int = _MAX_LIST_ITEMS) -> list[str]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            continue
        candidate = item.strip().lower()
        if not _SIGNAL_LABEL.fullmatch(candidate) or candidate in seen:
            continue
        seen.add(candidate)
        result.append(candidate)
        if len(result) >= limit:
            break
    return result


def _sanitize_scope(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _SCOPE_LABEL.fullmatch(candidate) else None


def _sanitize_execution_reason(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip().lower()
    if not stripped:
        return None
    mapped = _KNOWN_EXECUTION_REASONS.get(stripped)
    if mapped is not None:
        return mapped
    return stripped if _REASON_CODE.fullmatch(stripped) else None


def _synthesize_route_summary(route: Mapping[str, object]) -> str:
    prefix = "auto" if route.get("auto_selected") is True else "explicit"
    mode = str(route["mode"])
    band = route.get("band")
    score = route.get("difficulty_score")
    tiers = route.get("recommended_tiers")
    parts = [f"{prefix} {mode}"]
    if isinstance(band, str) and isinstance(score, (int, float)):
        parts.append(f"({band} {score:.2f})")
    elif isinstance(band, str):
        parts.append(f"({band})")
    if isinstance(tiers, list) and tiers:
        parts.append("-> " + ", ".join(str(t) for t in tiers[:6]))
    return " ".join(parts)


def sanitize_execution_route(fields: Mapping[str, object] | None) -> dict[str, object] | None:
    """Validate and bound a content-free ``execution_route`` payload.

    Rejects unknown modes and drops free text (including prompt snippets or
    arbitrary caller strings).  ``route_summary_line`` is always synthesized
    from validated fields so caller text can never leak through.
    """
    if not isinstance(fields, Mapping):
        return None
    raw_mode = fields.get("mode")
    if not isinstance(raw_mode, str):
        return None
    mode = raw_mode.strip().lower()
    if mode not in EXECUTION_MODES:
        return None

    route: dict[str, object] = {"mode": mode}

    raw_band = fields.get("band")
    if raw_band is None:
        raw_band = fields.get("difficulty_band")
    if isinstance(raw_band, str):
        band = raw_band.strip().lower()
        if band in DIFFICULTY_BANDS:
            route["band"] = band

    score = _bounded_float(fields.get("difficulty_score"), minimum=0.0, maximum=1.0)
    if score is not None:
        route["difficulty_score"] = score

    tiers = _bounded_label_list(fields.get("recommended_tiers"))
    if tiers:
        route["recommended_tiers"] = tiers

    raw_ens = fields.get("ensemble_mode")
    if isinstance(raw_ens, str):
        ens_mode = raw_ens.strip().lower()
        if ens_mode in ENSEMBLE_MODES:
            route["ensemble_mode"] = ens_mode

    scope = _sanitize_scope(fields.get("fanout_scope"))
    if scope is not None:
        route["fanout_scope"] = scope

    auto_selected = fields.get("auto_selected")
    if isinstance(auto_selected, bool):
        route["auto_selected"] = auto_selected

    reason = _sanitize_execution_reason(fields.get("reason"))
    if reason is not None:
        route["reason"] = reason

    signals = _bounded_signal_list(fields.get("signals"))
    if signals:
        route["signals"] = signals

    participants = _positive_int(fields.get("participant_count"))
    if participants is None and mode in ("ensemble", "fanout") and tiers:
        participants = len(tiers)
    if participants is not None:
        route["participant_count"] = participants

    route["route_summary_line"] = _synthesize_route_summary(route)
    return route


def _sanitize_timings(
    raw: Mapping[str, object] | None,
    *,
    prompt_tokens: int | None = None,
) -> dict[str, object]:
    """Validate bounded numeric inference timings and guard cache-hit rates."""
    if not isinstance(raw, Mapping):
        return {}
    cached = _non_negative_int(raw.get("prompt_cached_tokens"))
    if cached is None:
        cached = _non_negative_int(raw.get("backend_cached_tokens"))
    cached_invalid = (
        cached is not None and prompt_tokens is not None and cached > prompt_tokens
    )
    if cached_invalid:
        cached = None
    uncached = None if cached_invalid else _non_negative_int(raw.get("prompt_uncached_tokens"))
    if uncached is not None and prompt_tokens is not None and uncached > prompt_tokens:
        uncached = None
    if uncached is None and prompt_tokens is not None and cached is not None:
        uncached = prompt_tokens - cached
    if (
        uncached is not None
        and prompt_tokens is not None
        and cached is not None
        and uncached != prompt_tokens - cached
    ):
        uncached = prompt_tokens - cached

    ttft_ms = _bounded_float(raw.get("ttft_ms"), maximum=_MAX_DURATION_MS)
    prompt_eval_ms = _bounded_float(raw.get("prompt_eval_ms"), maximum=_MAX_DURATION_MS)
    eval_ms = _bounded_float(raw.get("eval_ms"), maximum=_MAX_DURATION_MS)
    load_ms = _bounded_float(raw.get("load_ms"), maximum=_MAX_DURATION_MS)
    queue_ms = _bounded_float(raw.get("queue_ms"), maximum=_MAX_DURATION_MS)
    backend_total_ms = _bounded_float(raw.get("backend_total_ms"), maximum=_MAX_DURATION_MS)

    prompt_eval_tps = _bounded_float(raw.get("prompt_eval_tps"), maximum=_MAX_RATE)
    if cached is not None and cached > 0:
        if uncached is not None and uncached > 0 and prompt_eval_ms is not None and prompt_eval_ms > 0.0:
            prompt_eval_tps = _bounded_float(
                uncached * 1000.0 / prompt_eval_ms,
                maximum=_MAX_RATE,
            )
        else:
            prompt_eval_tps = None

    decode_tps = _bounded_float(raw.get("decode_tps"), maximum=_MAX_RATE)
    if decode_tps is None:
        decode_tps = _bounded_float(
            raw.get("backend_predicted_tokens_per_second"),
            maximum=_MAX_RATE,
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


def _export_timing_attributes(timings: Mapping[str, object]) -> dict[str, object]:
    """Map sanitized timings onto Observatory's prefill/decode attribute names."""
    if not timings:
        return {}
    attrs: dict[str, object] = dict(timings)
    if "prompt_cached_tokens" in timings:
        attrs["backend_cached_tokens"] = timings["prompt_cached_tokens"]
    if "decode_tps" in timings:
        attrs["backend_predicted_tokens_per_second"] = timings["decode_tps"]
    return attrs


@dataclass
class _Attempt:
    number: int
    provider: str
    requested_model: str | None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error_code: str | None = None
    finished: bool = False
    timings: dict[str, object] = field(default_factory=dict)


@dataclass
class TelemetryTurn:
    """Per-turn state: one ``request.started`` and exactly one terminal event."""

    turn_id: str
    surface: str
    session_id: str | None
    work_run_id: str | None
    started_monotonic: float
    attempts: list[_Attempt] = field(default_factory=list)
    finished: bool = False
    # The RuntimeTelemetry that started the turn; route events go to its sink
    # even if another instance is the installed provider observer.
    owner: object | None = field(default=None, repr=False)
    announced_change: str | None = None
    execution_route: dict[str, object] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _begin_attempt(self, provider: str, requested_model: str | None) -> _Attempt:
        with self._lock:
            attempt = _Attempt(len(self.attempts) + 1, provider, requested_model)
            self.attempts.append(attempt)
            return attempt

    def _previous(self, attempt: _Attempt) -> _Attempt | None:
        with self._lock:
            index = attempt.number - 2
            return self.attempts[index] if index >= 0 else None

    def failed_attempts_code(self) -> str | None:
        """The last error code when the turn sent and every send failed, else None."""
        with self._lock:
            attempts = list(self.attempts)
        if not attempts or not all(a.finished and a.error_code for a in attempts):
            return None
        return attempts[-1].error_code

    def _summary(self) -> dict[str, object]:
        with self._lock:
            attempts = list(self.attempts)
            execution_route = (
                dict(self.execution_route) if self.execution_route is not None else None
            )
        last = attempts[-1] if attempts else None
        prompt = [a.prompt_tokens for a in attempts if a.prompt_tokens is not None]
        completion = [a.completion_tokens for a in attempts if a.completion_tokens is not None]
        summary: dict[str, object] = {
            "provider": last.provider if last else None,
            "model": (last.model or last.requested_model) if last else None,
            "attempts": len(attempts),
        }
        total_prompt = sum(prompt) if prompt else None
        total_completion = sum(completion) if completion else None
        if total_prompt is not None:
            summary["prompt_tokens"] = total_prompt
        if total_completion is not None:
            summary["completion_tokens"] = total_completion
        if total_prompt is not None or total_completion is not None:
            summary["token_counts_from_backend"] = True

        timed_attempts = [a for a in attempts if a.timings]
        if timed_attempts:
            agg: dict[str, object] = {}
            ttft_values = [
                a.timings["ttft_ms"]
                for a in timed_attempts
                if isinstance(a.timings.get("ttft_ms"), (int, float))
            ]
            if ttft_values:
                agg["ttft_ms"] = ttft_values[0]

            cached_values = [
                a.timings["prompt_cached_tokens"]
                for a in timed_attempts
                if type(a.timings.get("prompt_cached_tokens")) is int
            ]
            total_cached: int | None = None
            if cached_values:
                candidate_cached = sum(cached_values)
                if total_prompt is None or candidate_cached <= total_prompt:
                    total_cached = candidate_cached
                    agg["prompt_cached_tokens"] = total_cached

            uncached_values = [
                a.timings["prompt_uncached_tokens"]
                for a in timed_attempts
                if type(a.timings.get("prompt_uncached_tokens")) is int
            ]
            total_uncached: int | None = None
            if uncached_values:
                total_uncached = sum(uncached_values)
            elif total_prompt is not None and total_cached is not None:
                total_uncached = total_prompt - total_cached
            if total_uncached is not None:
                agg["prompt_uncached_tokens"] = total_uncached

            for dur_key in (
                "prompt_eval_ms",
                "eval_ms",
                "load_ms",
                "queue_ms",
                "backend_total_ms",
            ):
                dur_vals = [
                    float(a.timings[dur_key])
                    for a in timed_attempts
                    if isinstance(a.timings.get(dur_key), (int, float))
                ]
                if dur_vals:
                    summed = _bounded_float(sum(dur_vals), maximum=_MAX_DURATION_MS)
                    if summed is not None:
                        agg[dur_key] = summed

            prompt_tps_vals = [
                float(a.timings["prompt_eval_tps"])
                for a in timed_attempts
                if isinstance(a.timings.get("prompt_eval_tps"), (int, float))
            ]
            if len(timed_attempts) == 1 and prompt_tps_vals:
                agg["prompt_eval_tps"] = prompt_tps_vals[0]
            elif (
                total_uncached is not None
                and total_uncached > 0
                and isinstance(agg.get("prompt_eval_ms"), (int, float))
                and float(agg["prompt_eval_ms"]) > 0.0
            ):
                derived_prompt_tps = _bounded_float(
                    total_uncached * 1000.0 / float(agg["prompt_eval_ms"]),
                    maximum=_MAX_RATE,
                )
                if derived_prompt_tps is not None:
                    agg["prompt_eval_tps"] = derived_prompt_tps
            elif prompt_tps_vals and not (total_cached and total_uncached == 0):
                agg["prompt_eval_tps"] = prompt_tps_vals[-1]

            decode_tps_vals = [
                float(a.timings["decode_tps"])
                for a in timed_attempts
                if isinstance(a.timings.get("decode_tps"), (int, float))
            ]
            if len(timed_attempts) == 1 and decode_tps_vals:
                agg["decode_tps"] = decode_tps_vals[0]
            elif (
                total_completion is not None
                and total_completion > 0
                and isinstance(agg.get("eval_ms"), (int, float))
                and float(agg["eval_ms"]) > 0.0
            ):
                derived_decode_tps = _bounded_float(
                    total_completion * 1000.0 / float(agg["eval_ms"]),
                    maximum=_MAX_RATE,
                )
                if derived_decode_tps is not None:
                    agg["decode_tps"] = derived_decode_tps
            elif decode_tps_vals:
                agg["decode_tps"] = decode_tps_vals[-1]

            summary.update(_export_timing_attributes(agg))

        if execution_route is not None:
            summary["execution_route"] = execution_route
            summary["execution_mode"] = execution_route["mode"]
            if "band" in execution_route:
                summary["difficulty_band"] = execution_route["band"]
            if "difficulty_score" in execution_route:
                summary["difficulty_score"] = execution_route["difficulty_score"]
        return summary


_CURRENT_TURN: ContextVar[TelemetryTurn | None] = ContextVar(
    "sonder_runtime_telemetry_turn", default=None,
)


class RuntimeTelemetry:
    """Emit the Runtime v1 vocabulary into a redacting TelemetrySink."""

    def __init__(
        self,
        sink: TelemetrySink,
        *,
        session_id: str,
        version: str,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if sink is None or not callable(getattr(sink, "emit", None)):
            raise TypeError("a TelemetrySink is required")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id is required")
        self._sink = sink
        self._session_id = session_id
        self._version = str(version or "")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._failures = 0
        self._failure_lock = threading.Lock()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def emit_failures(self) -> int:
        with self._failure_lock:
            return self._failures

    # -- low-level emission -------------------------------------------------

    def _emit(
        self,
        event_type: str,
        attributes: Mapping[str, object],
        *,
        request_id: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        level: str = "INFO",
    ) -> None:
        try:
            self._sink.emit(TelemetryEvent(
                event_code=event_type,
                occurred_at=self._clock(),
                fields={k: v for k, v in attributes.items() if v is not None},
                correlation_id=request_id,
                run_id=run_id,
                session_id=session_id or self._session_id,
                level=level,
            ))
        except Exception:
            # Telemetry is never allowed to fail the operation it describes.
            with self._failure_lock:
                self._failures += 1

    # -- session ---------------------------------------------------------------

    def session_started(self, provider_bindings: Mapping[str, object]) -> None:
        self._emit("session.started", {
            "role": "runtime",
            "version": self._version,
            "text_capture": "none",
            "provider_bindings": {
                "default_generation_provider": provider_bindings.get("default_generation_provider"),
                "tier_providers": dict(provider_bindings.get("tier_providers") or {}),
                "embedding_provider": provider_bindings.get("embedding_provider"),
                "fallbacks": dict(provider_bindings.get("fallbacks") or {}),
            },
        })

    def session_ended(self, *, emitted_events: int, dropped_events: int) -> None:
        self._emit("session.ended", {
            "emitted_events": int(emitted_events),
            "dropped_events": int(dropped_events),
        })

    # -- turns -----------------------------------------------------------------

    def begin_turn(
        self,
        *,
        turn_id: str,
        surface: str,
        stream: bool,
        requested_model: object,
        source: str = "http",
        session_id: str | None = None,
        work_run_id: str | None = None,
    ) -> TelemetryTurn:
        """Start a turn and emit ``request.started``."""
        if surface not in SURFACES:
            raise ValueError("unknown telemetry surface %r" % surface)
        rid = sanitize_correlation_id(turn_id) or "turn-" + uuid.uuid4().hex
        turn = TelemetryTurn(
            turn_id=rid,
            surface=surface,
            session_id=sanitize_correlation_id(session_id),
            work_run_id=sanitize_correlation_id(work_run_id),
            started_monotonic=self._monotonic(),
            owner=self,
        )
        self._emit("request.started", {
            "surface": surface,
            "kind": "chat",
            "stream": bool(stream),
            "requested_model": bounded_label(requested_model),
            "workload": WORKLOAD_BY_SOURCE.get(source, "interactive_user"),
            "work_run_id": turn.work_run_id,
        }, request_id=rid, run_id=rid, session_id=turn.session_id)
        return turn

    @contextmanager
    def activate(self, turn: TelemetryTurn) -> Iterator[TelemetryTurn]:
        """Bind ``turn`` so provider sends on this thread are attributed to it."""
        token = _CURRENT_TURN.set(turn)
        try:
            yield turn
        finally:
            _CURRENT_TURN.reset(token)

    def finish_turn(
        self,
        turn: TelemetryTurn,
        *,
        outcome: str,
        http_status: int | None,
        error_code: str | None = None,
    ) -> bool:
        """Emit the single terminal ``request.*`` event; later calls are no-ops."""
        event_type = OUTCOMES.get(outcome)
        if event_type is None:
            raise ValueError("unknown turn outcome %r" % outcome)
        with turn._lock:
            if turn.finished:
                return False
            turn.finished = True
        summary = turn._summary()
        payload: dict[str, object] = {
            "outcome": outcome,
            "total_ms": max(0, int((self._monotonic() - turn.started_monotonic) * 1000)),
            "http_status": http_status if type(http_status) is int else None,
            "provider": summary["provider"],
            "model": bounded_label(summary["model"]),
            "attempts": summary["attempts"],
            "prompt_tokens": summary.get("prompt_tokens"),
            "completion_tokens": summary.get("completion_tokens"),
            "error_code": bounded_label(error_code) if outcome != "completed" else None,
            "work_run_id": turn.work_run_id,
        }
        for key in (
            "token_counts_from_backend",
            "ttft_ms",
            "prompt_cached_tokens",
            "backend_cached_tokens",
            "prompt_uncached_tokens",
            "prompt_eval_ms",
            "eval_ms",
            "load_ms",
            "queue_ms",
            "backend_total_ms",
            "prompt_eval_tps",
            "decode_tps",
            "backend_predicted_tokens_per_second",
            "execution_route",
            "execution_mode",
            "difficulty_band",
            "difficulty_score",
        ):
            if key in summary:
                payload[key] = summary[key]
        self._emit(event_type, payload,
                   request_id=turn.turn_id, run_id=turn.turn_id,
                   session_id=turn.session_id,
                   level="INFO" if outcome == "completed" else "WARNING")
        return True

    def reject_request(
        self,
        *,
        request_id: object,
        surface: str,
        requested_model: object,
        http_status: object,
        error_code: object,
        total_ms: object = None,
    ) -> None:
        """Emit one ``request.failed`` for a request refused before its turn began.

        There is no ``request.started``: a rejection is an error, never an
        open request span.  ``rejected: true`` tells it apart from a failed
        turn; ``attempts`` is 0 because nothing reached a provider.  The
        caller decides who may produce one (never an unauthenticated caller).
        """
        if surface not in SURFACES:
            raise ValueError(f"unknown telemetry surface {surface!r}")
        rid = sanitize_correlation_id(request_id) or "turn-" + uuid.uuid4().hex
        self._emit("request.failed", {
            "outcome": "failed",
            "rejected": True,
            "surface": surface,
            "kind": "chat",
            "http_status": http_status if type(http_status) is int else None,
            "error_code": bounded_label(error_code) or "rejected",
            "requested_model": bounded_label(requested_model),
            "attempts": 0,
            "total_ms": max(0, total_ms) if type(total_ms) is int else 0,
        }, request_id=rid, run_id=rid, level="WARNING")

    # -- dispatch_provider observer ---------------------------------------------

    def provider_send_started(self, provider_label, operation, model):
        """Observer hook: number the attempt inside the current turn."""
        turn = _CURRENT_TURN.get()
        if turn is None or turn.finished:
            return None
        attempt = turn._begin_attempt(provider_id(provider_label), bounded_label(model))
        return turn, attempt, str(operation or "")

    def provider_send_finished(
        self, handle, *, model=None, prompt_tokens=None, completion_tokens=None,
        error_code=None, timings=None,
    ) -> None:
        """Observer hook: emit ``route.changed`` (if any) and ``route.selected``."""
        if handle is None:
            return
        turn, attempt, operation = handle
        clean_prompt = _non_negative_int(prompt_tokens)
        clean_completion = _non_negative_int(completion_tokens)
        clean_timings = _sanitize_timings(timings, prompt_tokens=clean_prompt)
        with turn._lock:
            attempt.model = bounded_label(model) or attempt.requested_model
            attempt.prompt_tokens = clean_prompt
            attempt.completion_tokens = clean_completion
            attempt.error_code = bounded_label(error_code)
            attempt.timings = clean_timings
            attempt.finished = True
        previous = turn._previous(attempt)
        target = turn.owner if isinstance(turn.owner, RuntimeTelemetry) else self
        ids = {"request_id": turn.turn_id, "run_id": turn.turn_id,
               "session_id": turn.session_id}
        with turn._lock:
            announced = turn.announced_change == attempt.provider
            if announced:
                turn.announced_change = None
        if previous is not None and previous.provider != attempt.provider and not announced:
            target._emit("route.changed", {
                "from_provider": previous.provider,
                "to_provider": attempt.provider,
                "reason_code": previous.error_code or "reroute",
                "attempt": attempt.number,
            }, **ids)
        route_fields: dict[str, object] = {
            "provider": attempt.provider,
            "operation": "chat" if "chat" in operation else "generate",
            "model": attempt.model,
            "attempt": attempt.number,
            "status": "error" if attempt.error_code else "ok",
            "error_code": attempt.error_code,
        }
        if clean_timings:
            route_fields.update(_export_timing_attributes(clean_timings))
        target._emit("route.selected", route_fields, **ids)

    def provider_fallback(self, from_provider: str, to_provider: str, reason_code: str) -> None:
        """Emit ``route.changed`` for a pre-send fallback that never reached a send.

        A pre-send refusal (for example a cached not-ready health state) does
        not pass through dispatch_provider, so the fallback wrapper reports the
        change itself.
        """
        turn = _CURRENT_TURN.get()
        if turn is None or turn.finished:
            return
        target = turn.owner if isinstance(turn.owner, RuntimeTelemetry) else self
        destination = provider_id(to_provider)
        with turn._lock:
            upcoming = len(turn.attempts) + 1
            # The fallback send itself must not announce the same change again.
            turn.announced_change = destination
        target._emit("route.changed", {
            "from_provider": provider_id(from_provider),
            "to_provider": destination,
            "reason_code": bounded_label(reason_code) or "fallback",
            "attempt": upcoming,
        }, request_id=turn.turn_id, run_id=turn.turn_id, session_id=turn.session_id)

    def route_overflow(self, fields: Mapping) -> None:
        """Emit ``route.changed`` for a long-context overflow before its send.

        The overflow is the turn's first attempt, so no previous attempt
        exists for :meth:`provider_send_finished` to compare with; the rung
        reports the change itself.  Only labels and counts are exported.
        """
        turn = _CURRENT_TURN.get()
        if turn is None or turn.finished or not isinstance(fields, Mapping):
            return
        target = turn.owner if isinstance(turn.owner, RuntimeTelemetry) else self
        with turn._lock:
            upcoming = len(turn.attempts) + 1
        worker = bounded_label(fields.get("to_worker_id"))
        target._emit("route.changed", {
            **({"to_worker_id": worker} if worker else {}),
            "from_provider": provider_id(fields.get("from_provider")),
            "from_model": bounded_label(fields.get("from_model")),
            "to_provider": provider_id(fields.get("to_provider")),
            "to_model": bounded_label(fields.get("to_model")),
            "reason_code": bounded_label(fields.get("reason_code")) or "context_over_threshold",
            "estimated_tokens": _non_negative_int(fields.get("estimated_tokens")),
            "threshold": _non_negative_int(fields.get("threshold")),
            "attempt": upcoming,
        }, request_id=turn.turn_id, run_id=turn.turn_id, session_id=turn.session_id)

    def execution_route_decided(
        self,
        fields: Mapping[str, object] | None,
        *,
        request_id: str | None = None,
    ) -> dict[str, object] | None:
        """Record and emit content-free ``execution_route.decided`` telemetry."""
        route = sanitize_execution_route(fields)
        if route is None:
            return None
        turn = _CURRENT_TURN.get()
        if turn is not None and not turn.finished:
            with turn._lock:
                turn.execution_route = dict(route)
            target = turn.owner if isinstance(turn.owner, RuntimeTelemetry) else self
            rid = turn.turn_id
            run_id = turn.turn_id
            sid = turn.session_id
        else:
            rid = sanitize_correlation_id(request_id)
            if rid is None:
                return route
            target = self
            run_id = rid
            sid = None
        attrs: dict[str, object] = {
            "execution_route": dict(route),
            "execution_mode": route["mode"],
            "difficulty_band": route.get("band"),
            "difficulty_score": route.get("difficulty_score"),
            "auto_selected": route.get("auto_selected"),
            "recommended_tiers": route.get("recommended_tiers"),
            "route_summary_line": route.get("route_summary_line"),
        }
        if "participant_count" in route:
            attrs["participant_count"] = route["participant_count"]
        target._emit(
            "execution_route.decided",
            attrs,
            request_id=rid,
            run_id=run_id,
            session_id=sid,
        )
        return route

    def fanout_event(
        self,
        phase: str,
        fields: Mapping[str, object] | None = None,
        *,
        run_id: str | None = None,
    ) -> bool:
        """Emit a content-free ``fanout.<phase>`` lifecycle event."""
        norm_phase = str(phase or "").strip().lower()
        if norm_phase not in FANOUT_PHASES:
            return False
        turn = _CURRENT_TURN.get()
        clean_run_id = sanitize_correlation_id(run_id)
        if turn is not None and not turn.finished:
            target = turn.owner if isinstance(turn.owner, RuntimeTelemetry) else self
            req_id = turn.turn_id
            ev_run_id = clean_run_id or turn.turn_id
            sid = turn.session_id
        elif clean_run_id is not None:
            target = self
            req_id = clean_run_id
            ev_run_id = clean_run_id
            sid = None
        else:
            return False

        src = fields if isinstance(fields, Mapping) else {}
        scope = _sanitize_scope(src.get("scope")) or _sanitize_scope(src.get("fanout_scope"))
        tiers = _bounded_label_list(src.get("tiers"))
        models = _bounded_label_list(src.get("models"))
        worker_count = _positive_int(src.get("worker_count"))
        candidate_count = _positive_int(src.get("candidate_count"))
        participant_count = (
            _positive_int(src.get("participant_count"))
            or worker_count
            or candidate_count
            or (len(tiers) if tiers else None)
            or (len(models) if models else None)
        )
        status = bounded_label(src.get("status"))
        if status == _UNSAFE_LABEL:
            status = None
        error_code = bounded_label(src.get("error_code"))
        if error_code == _UNSAFE_LABEL:
            error_code = None
        served_model = bounded_label(src.get("served_model"))
        if served_model == _UNSAFE_LABEL:
            served_model = None
        done_reason = bounded_label(src.get("done_reason"))
        if done_reason == _UNSAFE_LABEL:
            done_reason = None

        fanout_summary: dict[str, object] = {}
        if scope is not None:
            fanout_summary["scope"] = scope
        if tiers:
            fanout_summary["tiers"] = tiers
        if models:
            fanout_summary["models"] = models
        if participant_count is not None:
            fanout_summary["count"] = participant_count
        if worker_count is not None:
            fanout_summary["workers"] = worker_count

        attrs: dict[str, object] = {
            "operation": "fanout",
            "fanout_scope": scope,
            "tiers": tiers or None,
            "models": models or None,
            "worker_count": worker_count or participant_count,
            "participant_count": participant_count,
            "completed_count": _non_negative_int(src.get("completed_count")),
            "succeeded_count": _non_negative_int(src.get("succeeded_count")),
            "failed_count": _non_negative_int(src.get("failed_count")),
            "elapsed_ms": _bounded_float(src.get("elapsed_ms"), maximum=_MAX_DURATION_MS),
            "status": status,
            "error_code": error_code,
            "tokens_in": _non_negative_int(src.get("tokens_in")),
            "tokens_out": _non_negative_int(src.get("tokens_out")),
            "served_model": served_model,
            "done_reason": done_reason,
        }
        if isinstance(src.get("auto_selected"), bool):
            attrs["auto_selected"] = src["auto_selected"]
        if isinstance(src.get("batch_job"), bool):
            attrs["batch_job"] = src["batch_job"]
        if fanout_summary:
            attrs["fanout"] = fanout_summary

        target._emit(
            "fanout." + norm_phase,
            attrs,
            request_id=req_id,
            run_id=ev_run_id,
            session_id=sid,
            level="WARNING" if norm_phase == "failed" else "INFO",
        )
        return True

    def ensemble_event(
        self,
        phase: str,
        fields: Mapping[str, object] | None = None,
        *,
        run_id: str | None = None,
    ) -> bool:
        """Emit a content-free ``ensemble.<phase>`` lifecycle event."""
        norm_phase = str(phase or "").strip().lower()
        if norm_phase not in ENSEMBLE_PHASES:
            return False
        turn = _CURRENT_TURN.get()
        clean_run_id = sanitize_correlation_id(run_id)
        if turn is not None and not turn.finished:
            target = turn.owner if isinstance(turn.owner, RuntimeTelemetry) else self
            req_id = turn.turn_id
            ev_run_id = clean_run_id or turn.turn_id
            sid = turn.session_id
        elif clean_run_id is not None:
            target = self
            req_id = clean_run_id
            ev_run_id = clean_run_id
            sid = None
        else:
            return False

        src = fields if isinstance(fields, Mapping) else {}
        raw_mode = src.get("mode") if src.get("mode") is not None else src.get("ensemble_mode")
        ens_mode = (
            raw_mode.strip().lower()
            if isinstance(raw_mode, str) and raw_mode.strip().lower() in ENSEMBLE_MODES
            else None
        )
        tiers = _bounded_label_list(src.get("tiers"))
        models = _bounded_label_list(src.get("models"))
        candidate_count = _positive_int(src.get("candidate_count"))
        participant_count = (
            _positive_int(src.get("participant_count"))
            or candidate_count
            or (len(tiers) if tiers else None)
            or (len(models) if models else None)
        )
        status = bounded_label(src.get("status"))
        if status == _UNSAFE_LABEL:
            status = None
        error_code = bounded_label(src.get("error_code"))
        if error_code == _UNSAFE_LABEL:
            error_code = None

        ensemble_summary: dict[str, object] = {}
        if ens_mode is not None:
            ensemble_summary["mode"] = ens_mode
        if tiers:
            ensemble_summary["tiers"] = tiers
        if models:
            ensemble_summary["models"] = models
        if participant_count is not None:
            ensemble_summary["count"] = participant_count

        attrs: dict[str, object] = {
            "operation": "ensemble",
            "ensemble_mode": ens_mode,
            "tiers": tiers or None,
            "models": models or None,
            "candidate_count": candidate_count or participant_count,
            "participant_count": participant_count,
            "succeeded_count": _non_negative_int(src.get("succeeded_count")),
            "failed_count": _non_negative_int(src.get("failed_count")),
            "elapsed_ms": _bounded_float(src.get("elapsed_ms"), maximum=_MAX_DURATION_MS),
            "status": status,
            "error_code": error_code,
        }
        if isinstance(src.get("auto_selected"), bool):
            attrs["auto_selected"] = src["auto_selected"]
        if isinstance(src.get("require_all_tiers"), bool):
            attrs["require_all_tiers"] = src["require_all_tiers"]
        if ensemble_summary:
            attrs["ensemble"] = ensemble_summary

        target._emit(
            "ensemble." + norm_phase,
            attrs,
            request_id=req_id,
            run_id=ev_run_id,
            session_id=sid,
            level="WARNING" if norm_phase == "failed" else "INFO",
        )
        return True


def current_turn() -> TelemetryTurn | None:
    """The telemetry turn bound on this thread, if any."""
    return _CURRENT_TURN.get()


__all__ = [
    "DIFFICULTY_BANDS",
    "ENSEMBLE_MODES",
    "ENSEMBLE_PHASES",
    "EXECUTION_MODES",
    "FANOUT_PHASES",
    "OUTCOMES",
    "PROVIDER_IDS",
    "RUNTIME_EVENT_VOCABULARY",
    "RUNTIME_EVENT_VOCABULARY_MAJOR",
    "RuntimeTelemetry",
    "SURFACES",
    "TelemetryTurn",
    "WORKLOAD_BY_SOURCE",
    "bounded_label",
    "current_turn",
    "provider_id",
    "sanitize_correlation_id",
    "sanitize_execution_route",
    "turn_id",
]
