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

_CORRELATION_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_LABEL = re.compile(r"[A-Za-z0-9._:/@+-]{1,96}")
_UNSAFE_LABEL = "[unsafe-label]"


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
    return value if type(value) is int and value >= 0 else None


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

    def _summary(self) -> dict[str, object]:
        with self._lock:
            attempts = list(self.attempts)
        last = attempts[-1] if attempts else None
        prompt = [a.prompt_tokens for a in attempts if a.prompt_tokens is not None]
        completion = [a.completion_tokens for a in attempts if a.completion_tokens is not None]
        summary: dict[str, object] = {
            "provider": last.provider if last else None,
            "model": (last.model or last.requested_model) if last else None,
            "attempts": len(attempts),
        }
        if prompt:
            summary["prompt_tokens"] = sum(prompt)
        if completion:
            summary["completion_tokens"] = sum(completion)
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
        self._emit(event_type, {
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
        }, request_id=turn.turn_id, run_id=turn.turn_id, session_id=turn.session_id,
            level="INFO" if outcome == "completed" else "WARNING")
        return True

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
        error_code=None,
    ) -> None:
        """Observer hook: emit ``route.changed`` (if any) and ``route.selected``."""
        if handle is None:
            return
        turn, attempt, operation = handle
        with turn._lock:
            attempt.model = bounded_label(model) or attempt.requested_model
            attempt.prompt_tokens = _non_negative_int(prompt_tokens)
            attempt.completion_tokens = _non_negative_int(completion_tokens)
            attempt.error_code = bounded_label(error_code)
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
        target._emit("route.selected", {
            "provider": attempt.provider,
            "operation": "chat" if "chat" in operation else "generate",
            "model": attempt.model,
            "attempt": attempt.number,
            "status": "error" if attempt.error_code else "ok",
            "error_code": attempt.error_code,
        }, **ids)

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


def current_turn() -> TelemetryTurn | None:
    """The telemetry turn bound on this thread, if any."""
    return _CURRENT_TURN.get()


__all__ = [
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
    "turn_id",
]
