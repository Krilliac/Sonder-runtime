"""Live token deltas on the early-committed SSE response of a chat turn.

``serve.py`` commits a plain streamed model turn to SSE before generating
(``_begin_early_stream``) and sends keep-alive comments while it runs.  This
module arms a :class:`~sonder_runtime.application.chat.stream_sink.LiveTurnStream`
for that turn, so a provider that can stream (Sonder Inference, through the
provider bridge) writes each content delta to the client as it is generated,
and writes the terminal frames once the turn is done:

* nothing was forwarded (an Ollama rung, a fallback, held-back code from the
  first token): the whole answer is sent as one delta, exactly as before;
* the final answer continues what was forwarded: only the remainder is sent;
* the final answer differs (a code-gate repair, an escalation, the web-denial
  guard): a visible revision notice and the final answer are sent, and the
  receipt says ``live_stream.revised``.

Every frame goes through the keep-alive writer's lock, so deltas and
keep-alive comments never interleave.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
import uuid
from collections.abc import Mapping

from sonder_runtime.application.chat import stream_sink
from sonder_runtime.application.chat import admission_wait_status as wait_status

_logger = logging.getLogger("sonder.serve")
# The chat code gate verifies fenced code after generation; code is held
# back until then instead of streamed (see stream_sink).
CODE_FENCE = "```"
_MAX_TOKEN_COUNT = 1_000_000_000
_MAX_DETAIL_KEYS = 16
_MAX_DETAIL_KEY_LEN = 64


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


def _normalize_usage(raw: object) -> dict[str, object] | None:
    """Normalize an OpenAI/Sonder usage mapping into bounded non-negative ints."""
    if not isinstance(raw, Mapping):
        return None
    prompt = _valid_token_count(raw.get("prompt_tokens"))
    if prompt is None:
        prompt = _valid_token_count(raw.get("input_tokens"))
    if prompt is None:
        prompt = _valid_token_count(raw.get("tokens_in"))
    completion = _valid_token_count(raw.get("completion_tokens"))
    if completion is None:
        completion = _valid_token_count(raw.get("output_tokens"))
    if completion is None:
        completion = _valid_token_count(raw.get("tokens_out"))
    total = _valid_token_count(raw.get("total_tokens"))
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
        raw.get("prompt_tokens_details"), max_cached=prompt_val,
    )
    if prompt_details:
        normalized["prompt_tokens_details"] = prompt_details
    completion_details = _clean_token_details(raw.get("completion_tokens_details"))
    if completion_details:
        normalized["completion_tokens_details"] = completion_details
    return normalized


def _has_positive_usage(usage: object) -> bool:
    if not isinstance(usage, Mapping):
        return False
    return any(
        (_valid_token_count(usage.get(key)) or 0) > 0
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    )


def _merge_detail_sums(first: object, second: object) -> dict[str, int]:
    merged: dict[str, int] = {}
    for source in (first, second):
        if not isinstance(source, Mapping):
            continue
        for key, value in source.items():
            count = _valid_token_count(value)
            if isinstance(key, str) and key and count is not None:
                merged[key] = min(_MAX_TOKEN_COUNT, merged.get(key, 0) + count)
    return merged


def _accumulate_usage(
    existing: dict[str, object] | None,
    incoming: dict[str, object],
) -> dict[str, object]:
    if existing is None:
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
    p_details = _merge_detail_sums(
        existing.get("prompt_tokens_details"),
        incoming.get("prompt_tokens_details"),
    )
    if "cached_tokens" in p_details and p_details["cached_tokens"] > prompt:
        p_details.pop("cached_tokens", None)
    if p_details:
        merged["prompt_tokens_details"] = p_details
    c_details = _merge_detail_sums(
        existing.get("completion_tokens_details"),
        incoming.get("completion_tokens_details"),
    )
    if c_details:
        merged["completion_tokens_details"] = c_details
    return merged


def _live_upstream_usage(live: object) -> dict[str, object] | None:
    if live is None:
        return None
    raw = getattr(live, "upstream_usage", None)
    return _normalize_usage(raw)


def _resolve_usage_for_stream(
    usage: object,
    live: object,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Return ``(sse_usage_chunk, receipt_usage)`` merging activity and live upstream counts.

    ``sse_usage_chunk`` stays ``None`` when ``usage`` was ``None`` (the caller did not
    request ``stream_options.include_usage``), and otherwise carries real non-zero
    upstream counts whenever activity tracking reported zeros (for example on tool
    turns that bypass ``_make_generate``). ``receipt_usage`` carries real observed
    counts for ``sonder_receipt`` whenever available.
    """
    upstream = _live_upstream_usage(live)
    if usage is None:
        return None, (dict(upstream) if _has_positive_usage(upstream) else None)
    passed = _normalize_usage(usage)
    if passed is None:
        if upstream is not None:
            return dict(upstream), dict(upstream)
        return (dict(usage) if isinstance(usage, Mapping) else None), None
    if upstream is None or not _has_positive_usage(upstream):
        return passed, (dict(passed) if _has_positive_usage(passed) else None)
    if not _has_positive_usage(passed):
        return dict(upstream), dict(upstream)
    # Activity already has positive counts (e.g. non-tool turn or multi-call turn);
    # backfill any zero side from upstream and carry through detail breakdowns when
    # the counts match the single streamed upstream call.
    p_prompt = _valid_token_count(passed.get("prompt_tokens")) or 0
    p_comp = _valid_token_count(passed.get("completion_tokens")) or 0
    u_prompt = _valid_token_count(upstream.get("prompt_tokens")) or 0
    u_comp = _valid_token_count(upstream.get("completion_tokens")) or 0
    prompt = p_prompt if p_prompt > 0 else u_prompt
    completion = p_comp if p_comp > 0 else u_comp
    p_total = _valid_token_count(passed.get("total_tokens")) or 0
    total = max(p_total, prompt + completion)
    resolved: dict[str, object] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    if p_prompt == u_prompt and p_comp == u_comp:
        for detail_key in ("prompt_tokens_details", "completion_tokens_details"):
            detail_val = upstream.get(detail_key)
            if isinstance(detail_val, Mapping) and detail_key not in passed:
                resolved[detail_key] = dict(detail_val)
    for detail_key in ("prompt_tokens_details", "completion_tokens_details"):
        detail_val = passed.get(detail_key)
        if isinstance(detail_val, Mapping) and detail_key not in resolved:
            resolved[detail_key] = dict(detail_val)
    return resolved, dict(resolved)


def _mark_close_connection(handler) -> None:
    try:
        handler.close_connection = True
    except AttributeError:
        pass


def _write_bytes(handler, data: bytes, *, early=None, flush: bool = False) -> bool:
    if early is not None and getattr(early, "client_gone", False):
        _mark_close_connection(handler)
        return False
    wfile = getattr(handler, "wfile", None)
    if wfile is None:
        if early is not None:
            early.client_gone = True
        _mark_close_connection(handler)
        return False
    try:
        wfile.write(data)
        if flush:
            flush_fn = getattr(wfile, "flush", None)
            if callable(flush_fn):
                flush_fn()
    except (OSError, ValueError):
        if early is not None:
            early.client_gone = True
        _mark_close_connection(handler)
        return False
    return True


def arm(handler, chunk, *, hold_code: bool, expose_reasoning: bool = False) -> stream_sink.LiveTurnStream | None:
    """A live stream bound to ``handler``'s early SSE response, or ``None``.

    The deltas carry a completion id minted here, before the turn has an
    interaction id; the early stream keeps it (``completion_id``) so the rest
    of the same SSE response (remainder, terminal, usage and error frames)
    reuses it.  ``expose_reasoning`` gates live ``reasoning_content`` deltas
    under the deployment's reasoning visibility policy.
    """
    handler._live_stream = None
    early = getattr(handler, "_early_stream", None)
    if early is None or early.client_gone:
        return None
    model = getattr(handler, "_stream_model", None) or "sonder"
    iid = early.completion_id = getattr(early, "completion_id", None) or uuid.uuid4().hex[:12]
    first = [True]

    def _send_delta(payload: dict) -> bool:
        with early.lock:
            if early.client_gone:
                _mark_close_connection(handler)
                return False
            delta = {"role": "assistant", **payload} if first[0] else payload
            frame = chunk(iid, model, delta).encode("utf-8")
            try:
                handler.wfile.write(frame)
                flush = getattr(handler.wfile, "flush", None)
                if callable(flush):
                    flush()
            except (OSError, ValueError):
                early.client_gone = True
                _mark_close_connection(handler)
                return False
            first[0] = False
        return True

    def write(text: str) -> bool:
        return _send_delta({"content": text})

    def write_reasoning(text: str) -> bool:
        return _send_delta({"reasoning_content": text})

    def write_tool_call(delta: dict) -> bool:
        return _send_delta({"tool_calls": [delta]})

    live = stream_sink.LiveTurnStream(
        write,
        write_reasoning=write_reasoning if expose_reasoning else None,
        write_tool_call=write_tool_call,
        client_gone=lambda: early.client_gone,
        hold_marker=CODE_FENCE if hold_code else None,
    )
    usage_lock = threading.Lock()
    live.upstream_usage = None  # type: ignore[attr-defined]

    def record_usage(raw_usage: object) -> None:
        normalized = _normalize_usage(raw_usage)
        if normalized is None:
            return
        with usage_lock:
            current = getattr(live, "upstream_usage", None)
            updated = _accumulate_usage(
                current if isinstance(current, dict) else None,
                normalized,
            )
            live.upstream_usage = updated  # type: ignore[attr-defined]

    live.record_usage = record_usage  # type: ignore[attr-defined]
    handler._live_stream = live
    return live


def wait_status_frame(line: str) -> bytes:
    """An SSE comment carrying ``line``; clients ignore it, raw viewers read it.

    An empty ``line`` is the clearing frame (``: sonder-status`` and nothing
    after it): the wait is over and the client drops the status text.
    """
    text = " ".join(str(line).split())[:300]
    return (": sonder-status" + (" " + text if text else "") + "\n\n").encode("utf-8")


def arm_wait_status(handler, *, interval_seconds: float = wait_status.DEFAULT_INTERVAL_SECONDS,
                    clock=time.monotonic) -> wait_status.AdmissionWaitReporter | None:
    """A reporter that writes GPU-admission wait lines to ``handler``'s early SSE."""
    early = getattr(handler, "_early_stream", None)
    if early is None or early.client_gone:
        return None

    def emit(line: str) -> None:
        frame = wait_status_frame(line)
        with early.lock:
            if early.client_gone:
                _mark_close_connection(handler)
                return
            try:
                handler.wfile.write(frame)
                flush = getattr(handler.wfile, "flush", None)
                if callable(flush):
                    flush()
            except (OSError, ValueError):
                early.client_gone = True
                _mark_close_connection(handler)

    return wait_status.AdmissionWaitReporter(
        emit, interval_seconds=interval_seconds, clock=clock)


@contextlib.contextmanager
def armed_for(handler, chunk, *, hold_code: bool, expose_reasoning: bool = False):
    """Arm a live stream for the enclosed turn when the response is early SSE."""
    live = arm(handler, chunk, hold_code=hold_code, expose_reasoning=expose_reasoning)
    reporter = arm_wait_status(handler)
    with stream_sink.armed(live), wait_status.armed(reporter):
        try:
            yield live
        except BaseException:
            if live is not None and live.cancelled:
                early = getattr(handler, "_early_stream", None)
                if early is not None:
                    early.client_gone = True
                _mark_close_connection(handler)
            raise


def response_id(handler, iid):
    """The ``chatcmpl-`` id every frame of ``handler``'s streamed response uses.

    Once live deltas went out on the early SSE response they fixed the id
    (minted in :func:`arm`, before the turn had an interaction id), so the
    remainder, terminal, usage and any error frame continue under it.  A
    response that forwarded nothing live uses ``iid``.  Read-only; called on
    the handler's own thread after the keep-alive writer has stopped.
    """
    early = getattr(handler, "_early_stream", None)
    live = getattr(handler, "_live_stream", None)
    if early is not None and live is not None and (live.forwarded or getattr(live, "any_forwarded", False)):
        return getattr(early, "completion_id", None) or iid
    return iid


def frame_id_and_receipt(handler, interaction_id, iid, receipt):
    """``(frame_id, receipt)`` for one streamed response of ``handler``.

    ``frame_id`` is :func:`response_id`. When live deltas fixed a different
    id before the turn had an interaction id (``interaction_id``), the
    returned receipt is a copy that names the interaction, so a client can
    still ``POST /v1/outcome`` for it; otherwise ``receipt`` is returned
    unchanged. Never mutates ``receipt``.
    """
    frame_id = response_id(handler, iid)
    if interaction_id and frame_id != interaction_id:
        receipt = {**(receipt or {}), "interaction_id": interaction_id}
    return frame_id, receipt


# Bound each SSE delta payload so large replies (such as inline base64 PNGs)
# never exceed client line-length guards (Flutter's defaultMaxStreamLineChars
# is 1 MiB) while short replies stay a single frame.
_MAX_STREAM_DELTA_CHARS = 65_536


def _write_content_deltas(handler, chunk, iid, model, text, *, include_role: bool, early=None) -> bool:
    if not text or len(text) <= _MAX_STREAM_DELTA_CHARS:
        delta = {"role": "assistant", "content": text} if include_role else {"content": text}
        frame = chunk(iid, model, delta).encode("utf-8")
        return _write_bytes(handler, frame, early=early)
    for offset in range(0, len(text), _MAX_STREAM_DELTA_CHARS):
        piece = text[offset:offset + _MAX_STREAM_DELTA_CHARS]
        delta = (
            {"role": "assistant", "content": piece}
            if (include_role and offset == 0)
            else {"content": piece}
        )
        frame = chunk(iid, model, delta).encode("utf-8")
        if not _write_bytes(handler, frame, early=early):
            return False
    return True


def write_stream_body(handler, chunk, iid, model, content, elapsed_ms, receipt,
                      usage, activity, lock=None):
    """Write the answer and terminal frames of a streamed turn; ``True`` when sent."""
    live = getattr(handler, "_live_stream", None) if lock is not None else None
    early = getattr(handler, "_early_stream", None)
    with lock if lock is not None else contextlib.nullcontext():
        if early is not None and getattr(early, "client_gone", False):
            _mark_close_connection(handler)
            return False
        any_live = live is not None and (live.forwarded or getattr(live, "any_forwarded", False))
        receipt_out = dict(receipt) if isinstance(receipt, Mapping) else {}
        if live is None or not live.forwarded:
            if any_live:
                iid = response_id(handler, iid)
            if not any_live or content:
                if not _write_content_deltas(
                    handler, chunk, iid, model, content,
                    include_role=not any_live, early=early,
                ):
                    return False
            if any_live:
                ttft = live.ttft_ms()
                receipt_out["live_stream"] = {
                    "revised": False,
                    **({"reasoning_streamed": True} if getattr(live, "reasoning_forwarded", "") else {}),
                    **({"tool_calls_streamed": True} if getattr(live, "tool_call_deltas_forwarded", 0) else {}),
                    **({"ttft_ms": int(ttft)} if ttft is not None else {}),
                }
        else:
            # One completion, one id: continue under the id the deltas used.
            iid = response_id(handler, iid)
            rest, revised = live.reconcile(content)
            if revised:
                _logger.warning("streamed answer was revised after generation; sent the final version")
            if rest:
                if not _write_content_deltas(
                    handler, chunk, iid, model, rest,
                    include_role=False, early=early,
                ):
                    return False
            ttft = live.ttft_ms()
            receipt_out["live_stream"] = {
                "revised": revised,
                **({"reasoning_streamed": True} if getattr(live, "reasoning_forwarded", "") else {}),
                **({"tool_calls_streamed": True} if getattr(live, "tool_call_deltas_forwarded", 0) else {}),
                **({"ttft_ms": int(ttft)} if ttft is not None else {}),
            }

        usage_live = live if live is not None else getattr(handler, "_live_stream", None)
        resolved_usage, receipt_usage = _resolve_usage_for_stream(usage, usage_live)
        if receipt_usage is not None and not _has_positive_usage(receipt_out.get("usage")):
            receipt_out["usage"] = dict(receipt_usage)

        activity_out = activity
        if (
            isinstance(activity, Mapping)
            and _has_positive_usage(receipt_usage)
            and (_valid_token_count(activity.get("tokens_in")) or 0) == 0
            and (_valid_token_count(activity.get("tokens_out")) or 0) == 0
        ):
            activity_out = dict(activity)
            activity_out["tokens_in"] = receipt_usage["prompt_tokens"]
            activity_out["tokens_out"] = receipt_usage["completion_tokens"]

        final_receipt = receipt_out if receipt_out else receipt
        terminal_frame = chunk(
            iid, model, {},
            finish_reason="length" if (final_receipt or {}).get("truncated") else "stop",
            elapsed_ms=elapsed_ms,
            receipt=final_receipt, activity=activity_out,
        ).encode("utf-8")
        if not _write_bytes(handler, terminal_frame, early=early):
            return False
        if resolved_usage is not None:
            usage_frame = chunk(iid, model, {}, usage=resolved_usage).encode("utf-8")
            if not _write_bytes(handler, usage_frame, early=early):
                return False
        if not _write_bytes(handler, b"data: [DONE]\n\n", early=early, flush=True):
            return False
    return True


__all__ = [
    "CODE_FENCE", "arm", "arm_wait_status", "armed_for", "frame_id_and_receipt",
    "response_id", "stream_admission", "wait_status_frame", "write_stream_body",
]

_admission_warned = False


def stream_admission(handler, total_seconds, enabled=True, *, admission=None):
    """Admit the model calls of a streamed turn as ``interactive_stream`` work.

    ``admission`` is the composition root's scope factory, called as
    ``admission(timeout_seconds=...)``; this module never imports the adapter.
    The scope's wait bound is the rest of the turn budget: ``total_seconds``
    less the time since the handler's ``_request_started``.  GPU admission ranks
    by caller name, so a streamed turn keeps interactive priority only through
    the ``interactive_stream`` caller.  A pool that does not know that caller
    yet gets no scope: the turn is then admitted as plain ``interactive`` work,
    exactly as before, never as background ``local`` work.  ``enabled`` false
    (a turn that is not streamed) or no ``admission`` adds no scope.
    """
    global _admission_warned
    stack = contextlib.ExitStack()
    if not enabled or admission is None:
        return stack
    started = getattr(handler, "_request_started", None)
    elapsed = 0.0 if started is None else max(0.0, time.monotonic() - started)
    try:
        # The pool validates the caller when the scope is entered.
        stack.enter_context(admission(timeout_seconds=max(0.0, float(total_seconds) - elapsed)))
    except ValueError:
        if not _admission_warned:
            _admission_warned = True
            _logger.warning("stream admission scope unavailable: pool rejects caller interactive_stream")
    return stack
