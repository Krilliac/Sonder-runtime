"""Streaming ``/v1/chat/completions`` transport for Sonder Inference.

Sonder Inference answers ``"stream": true`` with ``text/event-stream``
(Inference ``docs/SERVER.md``): ``chat.completion.chunk`` events carrying
``delta.content`` pieces, a final chunk with ``finish_reason``, ``timings``
and ``sonder`` (plus ``usage`` when ``stream_options.include_usage`` is set),
then ``data: [DONE]``.  A backend failure after chunks were sent arrives as
one ``data: {"error": ...}`` event and the stream ends without ``[DONE]``.

:func:`post_streaming` keeps the non-streaming transport's contract so the
gateway's error mapping, provider-attempt capture and response validation
are reused unchanged:

* the same direct, proxy-free, non-redirected exchange bounded by one
  wall-clock budget (the gateway's own budget machinery, passed in);
* a non-2xx status raises ``urllib.error.HTTPError`` carrying only the
  bounded error body, before any delta was forwarded;
* success returns one aggregated ``chat.completion`` object (the joined
  content, the served model, ``finish_reason``, ``usage``, ``timings`` and
  ``sonder``), exactly what the non-streaming route would have returned.

Each content delta is forwarded to the claimed
:class:`~sonder_runtime.application.chat.stream_sink.LiveTurnStream`.  When
the client goes away (a failed write, or the keep-alive writer noticed) the
upstream connection is shut down, which Sonder Inference treats as a client
disconnect and cancels the session, and :class:`Cancelled` is raised.  An
error event or a stream that ends early is :class:`DependencyUnavailable`:
the request executed, so it is never "unreachable" and never replayed.

Two optional stall limits sit inside the total wall-clock budget (see
:func:`stall_limits`; both are off unless a gateway sets them), so a slow
model can be given a long total budget and still fail fast when the upstream
stops talking:

* the **idle** limit: no bytes at all (keep-alive comments count as bytes)
  for this long, measured from the last bytes received once the response
  headers are in;
* the **first-content** limit: no content, reasoning or tool-call delta yet,
  measured from the start of the request.  Empty keep-alive deltas
  (``{"delta": {}}``, ``{"delta": {"role": ...}}``, comments) do not count.

Either one raises :class:`StreamTimeout` (a ``TimeoutError``) whose text names
the limit that fired and the elapsed numbers.

Known limits: the idle clock resets per complete line received, so one line
trickling in for longer than the limit counts as idle.  If the endpoint
answers a stream request with a single JSON body, the idle limit bounds the
whole body read after the headers, and the first-content limit then acts as a
cap on time-to-headers.
"""
from __future__ import annotations

import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

import http.client

from ...domain.common.errors import Cancelled, DependencyUnavailable
from ...platform.runtime_threads import Thread

# One SSE line (one chunk object) is small; anything larger is not a chunk.
MAX_LINE_BYTES = 1_048_576
CANCEL_POLL_SECONDS = 0.25
DONE = "[DONE]"
MAX_STALL_SECONDS = 86_400.0
STALL_POLL_SECONDS = 0.25


class StreamTimeout(TimeoutError):
    """A streamed exchange ran out of time; the text says which limit fired.

    ``kind`` is ``"total"`` (the outer wall-clock budget), ``"idle"`` or
    ``"first_content"``.  Gateways map it to ``DeadlineExceeded`` keeping the
    text (it carries no URL, header or credential).
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class StallLimits:
    """Seconds of silence tolerated; ``0`` disables a limit."""

    idle: float = 0.0
    first_content: float = 0.0
    idle_setting: str = "the stream idle timeout"
    first_content_setting: str = "the stream first-content timeout"

    def __post_init__(self) -> None:
        for name in ("idle", "first_content"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not (
                0.0 <= float(value) <= MAX_STALL_SECONDS
            ):
                raise ValueError("%s must be a number in [0, %d]" % (name, int(MAX_STALL_SECONDS)))

    @property
    def active(self) -> bool:
        return self.idle > 0 or self.first_content > 0


_LIMITS: ContextVar[StallLimits | None] = ContextVar("sonder_stream_stall_limits", default=None)


@contextmanager
def stall_limits(
    idle: float, first_content: float, *,
    idle_setting: str = "the stream idle timeout",
    first_content_setting: str = "the stream first-content timeout",
) -> Iterator[StallLimits]:
    """Apply stall limits to streamed exchanges made in this context.

    A scope rather than a transport argument so the injectable
    ``(url, payload, headers, timeout)`` transport seam keeps its shape.
    Scopes nest and always restore.
    """
    limits = StallLimits(idle, first_content, idle_setting, first_content_setting)
    token = _LIMITS.set(limits)
    try:
        yield limits
    finally:
        _LIMITS.reset(token)


def current_stall_limits() -> StallLimits:
    return _LIMITS.get() or StallLimits()


def _abort_exchange(budget) -> None:
    """Break a read that is blocked on the exchange's sockets."""
    budget._expire()  # shutdown(): unblocks a pending read on POSIX
    # Windows only aborts a blocking recv() on another thread when the
    # descriptor itself is closed.  The response's file object keeps the
    # socket open past ``socket.close()``, so detach the descriptor and
    # close that (the server then sees the disconnect and cancels).
    with budget._lock:
        sockets = list(budget._sockets)
    for sock in sockets:
        try:
            descriptor = sock.detach()
            if descriptor >= 0:
                socket.close(descriptor)
        except OSError:
            pass


class _StallWatch:
    """Abort the exchange when it goes silent or never produces content."""

    def __init__(self, budget, limits: StallLimits, started: float, label: str) -> None:
        self.fired: StreamTimeout | None = None
        self._budget = budget
        self._limits = limits
        self._started = started
        self._label = label
        self._last_bytes: float | None = None  # armed once the headers are in
        self._content = False
        self._stop = threading.Event()
        smallest = min(v for v in (limits.idle, limits.first_content) if v > 0)
        self._poll = max(0.005, min(STALL_POLL_SECONDS, smallest / 4.0))
        self._thread = Thread(target=self._run, name="sonder-stream-stall", daemon=True)
        self._thread.start()

    def arm_idle(self) -> None:
        self._last_bytes = time.monotonic()

    def touch(self) -> None:
        self._last_bytes = time.monotonic()

    def content(self) -> None:
        self._content = True

    def _run(self) -> None:
        while not self._stop.wait(self._poll):
            error = self._check(time.monotonic())
            if error is not None:
                self.fired = error
                _abort_exchange(self._budget)
                return

    def _check(self, now: float) -> StreamTimeout | None:
        limits = self._limits
        elapsed = now - self._started
        last = self._last_bytes
        if limits.idle > 0 and last is not None and now - last > limits.idle:
            return StreamTimeout("idle", (
                "%s stream idle timeout fired: no bytes for %.1fs (limit %.1fs, %s); "
                "%.1fs since the request started, %s"
            ) % (self._label, now - last, limits.idle, limits.idle_setting, elapsed,
                 "first content already received" if self._content
                 else "no content received yet"))
        if limits.first_content > 0 and not self._content and elapsed > limits.first_content:
            return StreamTimeout("first_content", (
                "%s stream first-content timeout fired: no content or reasoning delta "
                "after %.1fs (limit %.1fs, %s); bytes %s"
            ) % (self._label, elapsed, limits.first_content, limits.first_content_setting,
                 "were still arriving (keep-alives only)" if last is not None
                 else "had not started (no response headers)"))
        return None

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._poll + 1.0)


class _CancelWatch:
    """Shut the exchange down when the live client goes away."""

    def __init__(self, live, budget) -> None:
        self.cancelled = False
        self._live = live
        self._budget = budget
        self._stop = threading.Event()
        self._thread = None
        if live is not None:
            self._thread = Thread(
                target=self._run, name="sonder-inference-stream-cancel", daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(CANCEL_POLL_SECONDS):
            if self._live.cancelled:
                self.trip()
                return

    def trip(self) -> None:
        self.cancelled = True
        _abort_exchange(self._budget)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=CANCEL_POLL_SECONDS + 1.0)


def _malformed(detail: str, label: str = "sonder-inference") -> DependencyUnavailable:
    return DependencyUnavailable("%s sent a malformed event stream (%s)" % (label, detail))


class _Aggregate:
    """Fold chunk objects into one ``chat.completion`` document."""

    def __init__(self, live, progress=None, label: str = "sonder-inference") -> None:
        self._live = live
        self._progress = progress
        self._label = label
        self.parts: list[str] = []
        self.model: object = None
        self.ident: object = None
        self.finish_reason: object = None
        self.extras: dict[str, object] = {}
        self.done = False

    def event(self, data: str, error_fields) -> None:
        if data == DONE:
            self.done = True
            return
        try:
            chunk = json.loads(data)
        except (ValueError, RecursionError) as exc:
            raise _malformed("an event is not JSON", self._label) from exc
        if not isinstance(chunk, dict):
            raise _malformed("an event is not an object", self._label)
        if "error" in chunk and chunk.get("object") != "chat.completion.chunk":
            code, message = error_fields(chunk)
            raise DependencyUnavailable(
                "%s failed the request while streaming (%s)%s"
                % (self._label, code or "error", ": %s" % message if message else "")
            )
        for key, attr in (("model", "model"), ("id", "ident")):
            if chunk.get(key) is not None and getattr(self, attr) is None:
                setattr(self, attr, chunk[key])
        for key in ("usage", "timings", "sonder"):
            if key in chunk:
                self.extras[key] = chunk[key]
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return
        choice = choices[0]
        if not isinstance(choice, dict):
            raise _malformed("a choice is not an object", self._label)
        if choice.get("finish_reason") is not None:
            self.finish_reason = choice["finish_reason"]
        delta = choice.get("delta")
        piece = delta.get("content") if isinstance(delta, dict) else None
        if self._progress is not None and isinstance(delta, dict):
            counted = 0
            for key in ("content", "reasoning_content", "reasoning"):
                value = delta.get(key)
                if isinstance(value, str):
                    counted += len(value)
            if counted or delta.get("tool_calls"):
                self._progress.content()
        if isinstance(piece, str) and piece:
            self.parts.append(piece)
            if self._live is not None and not self._live.emit(piece):
                raise Cancelled("the streaming client disconnected; generation cancelled")

    def document(self) -> dict:
        document: dict[str, object] = {
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "".join(self.parts)},
                "finish_reason": self.finish_reason,
            }],
        }
        if self.ident is not None:
            document["id"] = self.ident
        if self.model is not None:
            document["model"] = self.model
        document.update(self.extras)
        return document


def consume_event_stream(
    stream, live, *, limit: int, error_fields, progress=None,
    label: str = "sonder-inference",
) -> dict:
    """Read an SSE body from ``stream`` into one aggregated completion.

    ``stream`` is a binary file-like with ``readline``.  ``limit`` bounds the
    whole body.  Raises :class:`Cancelled` when ``live`` reports the client
    gone and :class:`DependencyUnavailable` for an error event, a malformed
    or oversized stream, or a stream that ends before ``[DONE]``.  ``progress``
    (optional, ``touch()`` / ``content()``) is told about every read and
    every content-bearing delta so a stall watch can time the stream.
    """
    aggregate = _Aggregate(live, progress, label)
    data_lines: list[str] = []
    total = 0
    while not aggregate.done:
        raw = stream.readline(MAX_LINE_BYTES + 1)
        if not raw:
            break
        if progress is not None:
            progress.touch()
        total += len(raw)
        if total > limit:
            raise DependencyUnavailable("%s stream exceeds %d bytes" % (label, limit))
        if len(raw) > MAX_LINE_BYTES:
            raise _malformed("a line exceeds %d bytes" % MAX_LINE_BYTES, label)
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise _malformed("a line is not UTF-8", label) from exc
        if not line:
            if data_lines:
                aggregate.event("\n".join(data_lines), error_fields)
                data_lines = []
            continue
        if line.startswith(":"):
            continue  # comment / keep-alive
        name, _sep, value = line.partition(":")
        if name == "data":
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if data_lines and not aggregate.done:
        aggregate.event("\n".join(data_lines), error_fields)
    if not aggregate.done:
        raise DependencyUnavailable("%s stream ended before [DONE]" % label)
    return aggregate.document()


def post_streaming(
    url: str, payload: dict, headers, timeout, live, *, exchange,
    opener_factory=None, label: str = "sonder-inference",
) -> dict:
    """POST a ``"stream": true`` chat completion; see the module docstring.

    ``exchange`` is the Sonder Inference gateway module: its wall-clock
    budget, handlers, bounded reader, limits and error-document parser are
    reused (passed in rather than imported, so the modules stay acyclic).
    ``opener_factory(budget)`` replaces the default direct opener (the
    OpenAI-compatible gateway keeps its own proxy policy); ``label`` names the
    provider in error text.  Stall limits come from :func:`stall_limits`.
    """
    gateway = exchange
    budget_seconds = float(timeout) if timeout else gateway.DEFAULT_TIMEOUT_SECONDS
    data = json.dumps(payload).encode("utf-8")
    request_headers = dict(headers)
    request_headers["Accept"] = "text/event-stream"
    request = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
    limits = current_stall_limits()
    started = time.monotonic()
    with gateway._ExchangeBudget(budget_seconds) as budget:
        if opener_factory is not None:
            opener = opener_factory(budget)
        else:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), gateway._NoRedirect(),
                gateway._BudgetedHTTPHandler(budget), gateway._BudgetedHTTPSHandler(budget),
            )
        watch = _CancelWatch(live, budget)
        stall = _StallWatch(budget, limits, started, label) if limits.active else None
        try:
            try:
                response = opener.open(request, timeout=budget_seconds)
            except urllib.error.HTTPError as exc:
                try:
                    body = gateway._read_bounded(exc, gateway.ERROR_BODY_LIMIT)
                finally:
                    exc.close()
                raise urllib.error.HTTPError(
                    url, int(exc.code), "HTTP %d" % int(exc.code),
                    dict(exc.headers or {}), io.BytesIO(body),
                ) from None
            if stall is not None:
                stall.arm_idle()
            with response:
                content_type = str(response.headers.get("Content-Type", "")).lower()
                if "text/event-stream" not in content_type:
                    # A server that answered with one JSON document: the
                    # non-streaming contract, nothing to forward.  Content
                    # cannot arrive early, so only the idle limit applies.
                    if stall is not None:
                        stall.content()
                    body = gateway._read_bounded(response, gateway.RESPONSE_BODY_LIMIT)
                    if len(body) > gateway.RESPONSE_BODY_LIMIT:
                        raise DependencyUnavailable(
                            "%s response exceeds %d bytes" % (label, gateway.RESPONSE_BODY_LIMIT)
                        )
                    try:
                        return json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                        raise DependencyUnavailable(
                            "%s returned a non-JSON response" % label
                        ) from exc
                return consume_event_stream(
                    response, live, limit=gateway.RESPONSE_BODY_LIMIT,
                    error_fields=gateway._error_document_fields,
                    progress=stall, label=label,
                )
        except urllib.error.HTTPError:
            raise
        except Exception as exc:
            if watch.cancelled or isinstance(exc, Cancelled) or (
                live is not None and live.cancelled
            ):
                raise Cancelled(
                    "the streaming client disconnected; generation cancelled"
                ) from exc
            if stall is not None and stall.fired is not None:
                raise stall.fired from exc
            if budget.expired:
                raise StreamTimeout("total", (
                    "%s exchange exceeded its %.1fs budget (total timeout fired after %.1fs)"
                    % (label, budget_seconds, time.monotonic() - started)
                )) from exc
            if isinstance(exc, http.client.HTTPException):
                raise DependencyUnavailable(
                    "%s sent a malformed HTTP response (%s)" % (label, type(exc).__name__)
                ) from exc
            raise
        finally:
            if stall is not None:
                stall.stop()
            watch.stop()


__all__ = [
    "StallLimits", "StreamTimeout", "consume_event_stream", "current_stall_limits",
    "post_streaming", "stall_limits",
]
