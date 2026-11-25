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
from collections.abc import Mapping
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
_MAX_TOKEN_COUNT = 1_000_000_000
_MAX_DETAIL_KEYS = 16
_MAX_DETAIL_KEY_LEN = 64
_USAGE_COUNT_KEYS = frozenset({
    "prompt_tokens", "completion_tokens", "total_tokens",
    "input_tokens", "output_tokens",
})


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


def _close_budget_sockets(budget) -> None:
    """Shut down and close every socket attached to ``budget`` without altering timeout flags."""
    lock = getattr(budget, "_lock", None)
    if lock is not None:
        with lock:
            sockets = list(getattr(budget, "_sockets", ()))
    else:
        sockets = list(getattr(budget, "_sockets", ()))
    for sock in sockets:
        if sock is None:
            continue
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        try:
            detach = getattr(sock, "detach", None)
            if callable(detach):
                descriptor = detach()
                if isinstance(descriptor, int) and descriptor >= 0:
                    socket.close(descriptor)
            else:
                close = getattr(sock, "close", None)
                if callable(close):
                    close()
        except (OSError, ValueError):
            pass


def _abort_exchange(budget) -> None:
    """Break a read that is blocked on the exchange's sockets."""
    expire = getattr(budget, "_expire", None)
    if callable(expire):
        expire()  # shutdown(): unblocks a pending read on POSIX
    # Windows only aborts a blocking recv() on another thread when the
    # descriptor itself is closed.  The response's file object keeps the
    # socket open past ``socket.close()``, so detach the descriptor and
    # close that (the server then sees the disconnect and cancels).
    _close_budget_sockets(budget)


def _live_cancelled(live: object) -> bool:
    if live is None:
        return False
    try:
        return bool(getattr(live, "cancelled", False))
    except Exception:
        return False


def _valid_token_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= _MAX_TOKEN_COUNT else None


def _clean_token_details(raw: object, *, max_cached: int | None = None) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        return {}
    cleaned: dict[str, int] = {}
    for key, value in raw.items():
        if len(cleaned) >= _MAX_DETAIL_KEYS:
            break
        if (
            not isinstance(key, str)
            or not key
            or len(key) > _MAX_DETAIL_KEY_LEN
            or not key.isascii()
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in key)
        ):
            continue
        count = _valid_token_count(value)
        if count is None:
            continue
        if key == "cached_tokens" and max_cached is not None and count > max_cached:
            continue
        cleaned[key] = count
    return cleaned


def _merge_dict_extra(existing: object, incoming: object) -> object:
    """Merge non-null dict extras across chunks without letting null clobber data."""
    if incoming is None:
        return existing
    if not isinstance(incoming, dict):
        return existing if isinstance(existing, dict) else incoming
    if not isinstance(existing, dict):
        return dict(incoming)
    merged = dict(existing)
    for key, value in incoming.items():
        if value is None:
            continue
        prev = merged.get(key)
        if isinstance(prev, dict) and isinstance(value, dict):
            nested = dict(prev)
            nested.update({k: v for k, v in value.items() if v is not None})
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def _merge_usage_extra(existing: object, incoming: object) -> object:
    """Merge chunk ``usage`` fields without letting null or placeholder zeros clobber counts."""
    if incoming is None:
        return existing
    if not isinstance(incoming, dict):
        return existing if isinstance(existing, dict) else incoming
    if not isinstance(existing, dict):
        return dict(incoming)
    merged = dict(existing)
    for key, value in incoming.items():
        if value is None:
            continue
        if key in _USAGE_COUNT_KEYS:
            prev_count = _valid_token_count(merged.get(key))
            new_count = _valid_token_count(value)
            if new_count is None:
                if prev_count is None:
                    merged[key] = value
                continue
            if prev_count is not None and prev_count > 0 and new_count == 0:
                continue
            merged[key] = new_count
            continue
        prev = merged.get(key)
        if isinstance(prev, dict) and isinstance(value, dict):
            merged[key] = _merge_dict_extra(prev, value)
        else:
            merged[key] = value
    return merged


def _effective_timings(document: Mapping[str, object]) -> dict[str, object]:
    timings: dict[str, object] = {}
    raw_timings = document.get("timings")
    if isinstance(raw_timings, Mapping):
        timings.update({str(k): v for k, v in raw_timings.items() if v is not None})
    raw_usage = document.get("usage")
    if isinstance(raw_usage, Mapping):
        sonder_ext = raw_usage.get("sonder")
        if isinstance(sonder_ext, Mapping):
            nested = sonder_ext.get("timings")
            if isinstance(nested, Mapping):
                timings.update({str(k): v for k, v in nested.items() if v is not None})
    return timings


def _extract_normalized_usage(document: Mapping[str, object]) -> dict[str, object] | None:
    """Extract canonical bounded OpenAI token counts from an aggregated response."""
    raw_usage = document.get("usage")
    usage = raw_usage if isinstance(raw_usage, Mapping) else {}
    timings = _effective_timings(document)

    prompt = _valid_token_count(usage.get("prompt_tokens"))
    if prompt is None:
        prompt = _valid_token_count(usage.get("input_tokens"))
    timing_prompt = _valid_token_count(timings.get("prompt_n"))
    if (prompt is None or prompt == 0) and timing_prompt is not None and (
        prompt is None or timing_prompt > 0
    ):
        prompt = timing_prompt

    completion = _valid_token_count(usage.get("completion_tokens"))
    if completion is None:
        completion = _valid_token_count(usage.get("output_tokens"))
    timing_completion = _valid_token_count(timings.get("predicted_n"))
    if (completion is None or completion == 0) and timing_completion is not None and (
        completion is None or timing_completion > 0
    ):
        completion = timing_completion

    total = _valid_token_count(usage.get("total_tokens"))
    if prompt is None and completion is None and total is None:
        return None
    prompt_val = prompt or 0
    completion_val = completion or 0
    computed_total = prompt_val + completion_val
    total_val = total if (total is not None and total >= computed_total) else computed_total
    normalized: dict[str, object] = {
        "prompt_tokens": prompt_val,
        "completion_tokens": completion_val,
        "total_tokens": total_val,
    }
    prompt_details = _clean_token_details(
        usage.get("prompt_tokens_details"), max_cached=prompt_val,
    )
    if "cached_tokens" not in prompt_details:
        cache_n = _valid_token_count(timings.get("cache_n"))
        if cache_n is not None and cache_n <= prompt_val:
            prompt_details["cached_tokens"] = cache_n
    if prompt_details:
        normalized["prompt_tokens_details"] = prompt_details
    completion_details = _clean_token_details(usage.get("completion_tokens_details"))
    if completion_details:
        normalized["completion_tokens_details"] = completion_details
    return normalized


def _accumulate_usage(
    existing: object,
    incoming: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(existing, Mapping):
        return dict(incoming)
    prompt = min(
        _MAX_TOKEN_COUNT,
        (_valid_token_count(existing.get("prompt_tokens")) or 0)
        + (_valid_token_count(incoming.get("prompt_tokens")) or 0),
    )
    completion = min(
        _MAX_TOKEN_COUNT,
        (_valid_token_count(existing.get("completion_tokens")) or 0)
        + (_valid_token_count(incoming.get("completion_tokens")) or 0),
    )
    existing_total = _valid_token_count(existing.get("total_tokens")) or 0
    incoming_total = _valid_token_count(incoming.get("total_tokens")) or 0
    total = min(_MAX_TOKEN_COUNT, max(existing_total + incoming_total, prompt + completion))
    merged: dict[str, object] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    for detail_key in ("prompt_tokens_details", "completion_tokens_details"):
        combined: dict[str, int] = {}
        for source in (existing.get(detail_key), incoming.get(detail_key)):
            if not isinstance(source, Mapping):
                continue
            for key, value in source.items():
                count = _valid_token_count(value)
                if isinstance(key, str) and key and count is not None:
                    combined[key] = min(_MAX_TOKEN_COUNT, combined.get(key, 0) + count)
        if detail_key == "prompt_tokens_details" and combined.get("cached_tokens", 0) > prompt:
            combined.pop("cached_tokens", None)
        if combined:
            merged[detail_key] = combined
    return merged


def _report_live_usage(live, document: object) -> None:
    if live is None or not isinstance(document, Mapping):
        return
    normalized = _extract_normalized_usage(document)
    if normalized is None:
        return
    recorder = getattr(live, "record_usage", None)
    if callable(recorder):
        recorder(dict(normalized))
        return
    inner = getattr(live, "_live", None)
    inner_recorder = getattr(inner, "record_usage", None) if inner is not None else None
    if callable(inner_recorder):
        inner_recorder(dict(normalized))
        return
    try:
        current = getattr(live, "upstream_usage", None)
        live.upstream_usage = _accumulate_usage(current, normalized)
    except AttributeError:
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
        self._trip_lock = threading.Lock()
        self._tripped = False
        self._stop = threading.Event()
        self._thread = None
        if live is not None:
            if _live_cancelled(live):
                self.trip()
            else:
                self._thread = Thread(
                    target=self._run, name="sonder-inference-stream-cancel", daemon=True,
                )
                self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(CANCEL_POLL_SECONDS):
            if _live_cancelled(self._live):
                self.trip()
                return

    def trip(self) -> None:
        with self._trip_lock:
            self.cancelled = True
            if self._tripped:
                return
            self._tripped = True
        abort = getattr(self._budget, "abort", None)
        if callable(abort):
            abort()
        else:
            _abort_exchange(self._budget)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and threading.current_thread() is not self._thread:
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
        self.reasoning_parts: list[str] = []
        self._tool_calls: dict[int, dict[str, object]] = {}
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
        if "usage" in chunk:
            merged_usage = _merge_usage_extra(self.extras.get("usage"), chunk["usage"])
            if merged_usage is not None:
                self.extras["usage"] = merged_usage
        for key in ("timings", "sonder"):
            if key in chunk:
                merged_extra = _merge_dict_extra(self.extras.get(key), chunk[key])
                if merged_extra is not None:
                    self.extras[key] = merged_extra
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
        reasoning_piece = None
        tool_call_deltas = None
        if isinstance(delta, dict):
            raw_reasoning = delta.get("reasoning_content")
            if not isinstance(raw_reasoning, str) or not raw_reasoning:
                raw_reasoning = delta.get("reasoning")
            if isinstance(raw_reasoning, str) and raw_reasoning:
                reasoning_piece = raw_reasoning
            raw_tool_calls = delta.get("tool_calls")
            if isinstance(raw_tool_calls, list) and raw_tool_calls:
                tool_call_deltas = raw_tool_calls
        if self._progress is not None and isinstance(delta, dict):
            counted = 0
            for key in ("content", "reasoning_content", "reasoning"):
                value = delta.get(key)
                if isinstance(value, str):
                    counted += len(value)
            if counted or delta.get("tool_calls"):
                self._progress.content()
        if reasoning_piece is not None:
            self.reasoning_parts.append(reasoning_piece)
            if self._live is not None:
                emit_reasoning = getattr(self._live, "emit_reasoning", None)
                if callable(emit_reasoning) and not emit_reasoning(reasoning_piece):
                    raise Cancelled("the streaming client disconnected; generation cancelled")
        if tool_call_deltas is not None:
            for item in tool_call_deltas:
                if not isinstance(item, dict):
                    continue
                raw_idx = item.get("index", 0)
                if isinstance(raw_idx, bool) or not isinstance(raw_idx, int) or raw_idx < 0 or raw_idx >= 64:
                    continue
                entry = self._tool_calls.setdefault(
                    raw_idx,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if isinstance(item.get("id"), str) and item["id"]:
                    entry["id"] = item["id"]
                if isinstance(item.get("type"), str) and item["type"]:
                    entry["type"] = item["type"]
                fn = item.get("function")
                if isinstance(fn, dict):
                    dst_fn = entry["function"]
                    if isinstance(dst_fn, dict):
                        if isinstance(fn.get("name"), str) and fn["name"]:
                            dst_fn["name"] = fn["name"]
                        if isinstance(fn.get("arguments"), str) and fn["arguments"]:
                            dst_fn["arguments"] = str(dst_fn.get("arguments") or "") + fn["arguments"]
                if self._live is not None:
                    emit_tc = getattr(self._live, "emit_tool_call_delta", None)
                    if callable(emit_tc) and not emit_tc(item):
                        raise Cancelled("the streaming client disconnected; generation cancelled")
        if isinstance(piece, str) and piece:
            self.parts.append(piece)
            if self._live is not None and not self._live.emit(piece):
                raise Cancelled("the streaming client disconnected; generation cancelled")

    def document(self) -> dict:
        message: dict[str, object] = {"role": "assistant", "content": "".join(self.parts)}
        if self.reasoning_parts:
            message["reasoning_content"] = "".join(self.reasoning_parts)
        if self._tool_calls:
            message["tool_calls"] = [self._tool_calls[idx] for idx in sorted(self._tool_calls)]
        document: dict[str, object] = {
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": self.finish_reason,
            }],
        }
        if self.ident is not None:
            document["id"] = self.ident
        if self.model is not None:
            document["model"] = self.model
        document.update(self.extras)
        raw_usage = document.get("usage")
        if isinstance(raw_usage, dict):
            normalized = _extract_normalized_usage(document)
            if normalized is not None:
                merged_usage = dict(raw_usage)
                merged_usage["prompt_tokens"] = normalized["prompt_tokens"]
                merged_usage["completion_tokens"] = normalized["completion_tokens"]
                merged_usage["total_tokens"] = normalized["total_tokens"]
                for detail_key in ("prompt_tokens_details", "completion_tokens_details"):
                    if detail_key in normalized and not isinstance(merged_usage.get(detail_key), dict):
                        merged_usage[detail_key] = normalized[detail_key]
                document["usage"] = merged_usage
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
        if _live_cancelled(live):
            raise Cancelled("the streaming client disconnected; generation cancelled")
        raw = stream.readline(MAX_LINE_BYTES + 1)
        if not raw:
            break
        if _live_cancelled(live):
            raise Cancelled("the streaming client disconnected; generation cancelled")
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
    document = aggregate.document()
    _report_live_usage(live, document)
    return document


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
    if _live_cancelled(live):
        raise Cancelled("the streaming client disconnected; generation cancelled")
    gateway = exchange
    budget_seconds = float(timeout) if timeout else gateway.DEFAULT_TIMEOUT_SECONDS
    data = json.dumps(payload).encode("utf-8")
    request_headers = dict(headers)
    request_headers["Accept"] = "text/event-stream"
    request = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
    limits = current_stall_limits()
    started = time.monotonic()
    with gateway._ExchangeBudget(budget_seconds) as budget:
        if getattr(budget, "aborted", False):
            raise Cancelled("the operation was cancelled; generation stopped")
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
            if watch.cancelled or _live_cancelled(live):
                watch.trip()
                raise Cancelled("the streaming client disconnected; generation cancelled")
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
                if watch.cancelled or _live_cancelled(live):
                    watch.trip()
                    raise Cancelled("the streaming client disconnected; generation cancelled")
                if getattr(budget, "aborted", False):
                    raise Cancelled("the operation was cancelled; generation stopped")
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
                        document = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                        raise DependencyUnavailable(
                            "%s returned a non-JSON response" % label
                        ) from exc
                    _report_live_usage(live, document)
                    return document
                return consume_event_stream(
                    response, live, limit=gateway.RESPONSE_BODY_LIMIT,
                    error_fields=gateway._error_document_fields,
                    progress=stall, label=label,
                )
        except urllib.error.HTTPError:
            raise
        except Exception as exc:
            if watch.cancelled or isinstance(exc, Cancelled) or _live_cancelled(live):
                watch.trip()
                raise Cancelled(
                    "the streaming client disconnected; generation cancelled"
                ) from exc
            if getattr(budget, "aborted", False):
                # The operation's cancellation token closed the exchange.
                _close_budget_sockets(budget)
                raise Cancelled("the operation was cancelled; generation stopped") from exc
            if stall is not None and stall.fired is not None:
                _close_budget_sockets(budget)
                raise stall.fired from exc
            if budget.expired:
                _close_budget_sockets(budget)
                raise StreamTimeout("total", (
                    "%s exchange exceeded its %.1fs budget (total timeout fired after %.1fs)"
                    % (label, budget_seconds, time.monotonic() - started)
                )) from exc
            _close_budget_sockets(budget)
            if isinstance(exc, http.client.HTTPException):
                raise DependencyUnavailable(
                    "%s sent a malformed HTTP response (%s)" % (label, type(exc).__name__)
                ) from exc
            raise
        except BaseException:
            _close_budget_sockets(budget)
            raise
        finally:
            if stall is not None:
                stall.stop()
            watch.stop()


__all__ = [
    "StallLimits", "StreamTimeout", "consume_event_stream", "current_stall_limits",
    "post_streaming", "stall_limits",
]
