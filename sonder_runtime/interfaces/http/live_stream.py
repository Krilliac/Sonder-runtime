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
import time
import uuid

from sonder_runtime.application.chat import stream_sink
from sonder_runtime.application.chat import admission_wait_status as wait_status

_logger = logging.getLogger("sonder.serve")
# The chat code gate verifies fenced code after generation; code is held
# back until then instead of streamed (see stream_sink).
CODE_FENCE = "```"


def arm(handler, chunk, *, hold_code: bool) -> stream_sink.LiveTurnStream | None:
    """A live stream bound to ``handler``'s early SSE response, or ``None``.

    The deltas carry a completion id minted here, before the turn has an
    interaction id; the early stream keeps it (``completion_id``) so the rest
    of the same SSE response (remainder, terminal, usage and error frames)
    reuses it.
    """
    handler._live_stream = None
    early = getattr(handler, "_early_stream", None)
    if early is None or early.client_gone:
        return None
    model = getattr(handler, "_stream_model", None) or "sonder"
    iid = early.completion_id = getattr(early, "completion_id", None) or uuid.uuid4().hex[:12]
    first = [True]

    def write(text: str) -> bool:
        delta = {"role": "assistant", "content": text} if first[0] else {"content": text}
        frame = chunk(iid, model, delta).encode("utf-8")
        with early.lock:
            if early.client_gone:
                return False
            try:
                handler.wfile.write(frame)
                flush = getattr(handler.wfile, "flush", None)
                if callable(flush):
                    flush()
            except (OSError, ValueError):
                early.client_gone = True
                return False
        first[0] = False
        return True

    live = stream_sink.LiveTurnStream(
        write, client_gone=lambda: early.client_gone,
        hold_marker=CODE_FENCE if hold_code else None,
    )
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
                return
            try:
                handler.wfile.write(frame)
                flush = getattr(handler.wfile, "flush", None)
                if callable(flush):
                    flush()
            except (OSError, ValueError):
                early.client_gone = True

    return wait_status.AdmissionWaitReporter(
        emit, interval_seconds=interval_seconds, clock=clock)


@contextlib.contextmanager
def armed_for(handler, chunk, *, hold_code: bool):
    """Arm a live stream for the enclosed turn when the response is early SSE."""
    with stream_sink.armed(arm(handler, chunk, hold_code=hold_code)) as live, \
            wait_status.armed(arm_wait_status(handler)):
        yield live


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
    if early is not None and live is not None and live.forwarded:
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


def _write_content_deltas(handler, chunk, iid, model, text, *, include_role: bool):
    if not text or len(text) <= _MAX_STREAM_DELTA_CHARS:
        delta = {"role": "assistant", "content": text} if include_role else {"content": text}
        handler.wfile.write(chunk(iid, model, delta).encode("utf-8"))
        return
    for offset in range(0, len(text), _MAX_STREAM_DELTA_CHARS):
        piece = text[offset:offset + _MAX_STREAM_DELTA_CHARS]
        delta = (
            {"role": "assistant", "content": piece}
            if (include_role and offset == 0)
            else {"content": piece}
        )
        handler.wfile.write(chunk(iid, model, delta).encode("utf-8"))


def write_stream_body(handler, chunk, iid, model, content, elapsed_ms, receipt,
                      usage, activity, lock=None):
    """Write the answer and terminal frames of a streamed turn; ``True`` when sent."""
    live = getattr(handler, "_live_stream", None) if lock is not None else None
    with lock if lock is not None else contextlib.nullcontext():
        if live is None or not live.forwarded:
            _write_content_deltas(
                handler, chunk, iid, model, content, include_role=True,
            )
        else:
            # One completion, one id: continue under the id the deltas used.
            iid = response_id(handler, iid)
            rest, revised = live.reconcile(content)
            if revised:
                _logger.warning("streamed answer was revised after generation; sent the final version")
            if rest:
                _write_content_deltas(
                    handler, chunk, iid, model, rest, include_role=False,
                )
            ttft = live.ttft_ms()
            receipt = dict(receipt or {})
            receipt["live_stream"] = {
                "revised": revised,
                **({"ttft_ms": int(ttft)} if ttft is not None else {}),
            }
        handler.wfile.write(chunk(
            iid, model, {}, finish_reason="length" if (receipt or {}).get("truncated") else "stop",
            elapsed_ms=elapsed_ms,
            receipt=receipt, activity=activity,
        ).encode("utf-8"))
        if usage is not None:
            handler.wfile.write(chunk(iid, model, {}, usage=usage).encode("utf-8"))
        handler.wfile.write(b"data: [DONE]\n\n")
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
