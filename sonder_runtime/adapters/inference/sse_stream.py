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
"""
from __future__ import annotations

import io
import json
import socket
import threading
import urllib.error
import urllib.request

import http.client

from ...domain.common.errors import Cancelled, DependencyUnavailable
from ...platform.runtime_threads import Thread

# One SSE line (one chunk object) is small; anything larger is not a chunk.
MAX_LINE_BYTES = 1_048_576
CANCEL_POLL_SECONDS = 0.25
DONE = "[DONE]"


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
        self._budget._expire()  # shutdown(): unblocks a pending read on POSIX
        # Windows only aborts a blocking recv() on another thread when the
        # descriptor itself is closed.  The response's file object keeps the
        # socket open past ``socket.close()``, so detach the descriptor and
        # close that (the server then sees the disconnect and cancels).
        with self._budget._lock:
            sockets = list(self._budget._sockets)
        for sock in sockets:
            try:
                descriptor = sock.detach()
                if descriptor >= 0:
                    socket.close(descriptor)
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=CANCEL_POLL_SECONDS + 1.0)


def _malformed(detail: str) -> DependencyUnavailable:
    return DependencyUnavailable("sonder-inference sent a malformed event stream (%s)" % detail)


class _Aggregate:
    """Fold chunk objects into one ``chat.completion`` document."""

    def __init__(self, live) -> None:
        self._live = live
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
            raise _malformed("an event is not JSON") from exc
        if not isinstance(chunk, dict):
            raise _malformed("an event is not an object")
        if "error" in chunk and chunk.get("object") != "chat.completion.chunk":
            code, message = error_fields(chunk)
            raise DependencyUnavailable(
                "sonder-inference failed the request while streaming (%s)%s"
                % (code or "error", ": %s" % message if message else "")
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
            raise _malformed("a choice is not an object")
        if choice.get("finish_reason") is not None:
            self.finish_reason = choice["finish_reason"]
        delta = choice.get("delta")
        piece = delta.get("content") if isinstance(delta, dict) else None
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


def consume_event_stream(stream, live, *, limit: int, error_fields) -> dict:
    """Read an SSE body from ``stream`` into one aggregated completion.

    ``stream`` is a binary file-like with ``readline``.  ``limit`` bounds the
    whole body.  Raises :class:`Cancelled` when ``live`` reports the client
    gone and :class:`DependencyUnavailable` for an error event, a malformed
    or oversized stream, or a stream that ends before ``[DONE]``.
    """
    aggregate = _Aggregate(live)
    data_lines: list[str] = []
    total = 0
    while not aggregate.done:
        raw = stream.readline(MAX_LINE_BYTES + 1)
        if not raw:
            break
        total += len(raw)
        if total > limit:
            raise DependencyUnavailable("sonder-inference stream exceeds %d bytes" % limit)
        if len(raw) > MAX_LINE_BYTES:
            raise _malformed("a line exceeds %d bytes" % MAX_LINE_BYTES)
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise _malformed("a line is not UTF-8") from exc
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
        raise DependencyUnavailable("sonder-inference stream ended before [DONE]")
    return aggregate.document()


def post_streaming(url: str, payload: dict, headers, timeout, live, *, exchange) -> dict:
    """POST a ``"stream": true`` chat completion; see the module docstring.

    ``exchange`` is the Sonder Inference gateway module: its wall-clock
    budget, handlers, bounded reader, limits and error-document parser are
    reused (passed in rather than imported, so the modules stay acyclic).
    """
    gateway = exchange
    budget_seconds = float(timeout) if timeout else gateway.DEFAULT_TIMEOUT_SECONDS
    data = json.dumps(payload).encode("utf-8")
    request_headers = dict(headers)
    request_headers["Accept"] = "text/event-stream"
    request = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
    with gateway._ExchangeBudget(budget_seconds) as budget:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), gateway._NoRedirect(),
            gateway._BudgetedHTTPHandler(budget), gateway._BudgetedHTTPSHandler(budget),
        )
        watch = _CancelWatch(live, budget)
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
            with response:
                content_type = str(response.headers.get("Content-Type", "")).lower()
                if "text/event-stream" not in content_type:
                    # A server that answered with one JSON document: the
                    # non-streaming contract, nothing to forward.
                    body = gateway._read_bounded(response, gateway.RESPONSE_BODY_LIMIT)
                    if len(body) > gateway.RESPONSE_BODY_LIMIT:
                        raise DependencyUnavailable(
                            "sonder-inference response exceeds %d bytes" % gateway.RESPONSE_BODY_LIMIT
                        )
                    try:
                        return json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                        raise DependencyUnavailable(
                            "sonder-inference returned a non-JSON response"
                        ) from exc
                return consume_event_stream(
                    response, live, limit=gateway.RESPONSE_BODY_LIMIT,
                    error_fields=gateway._error_document_fields,
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
            if budget.expired:
                raise TimeoutError(
                    "sonder-inference exchange exceeded its %.1fs budget" % budget_seconds
                ) from exc
            if isinstance(exc, http.client.HTTPException):
                raise DependencyUnavailable(
                    "sonder-inference sent a malformed HTTP response (%s)" % type(exc).__name__
                ) from exc
            raise
        finally:
            watch.stop()


__all__ = ["consume_event_stream", "post_streaming"]
