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

try:
    from sonder_runtime.application.chat import admission_wait_status as _wait_status
except ImportError:  # sibling w2/inference-pool owns admission_wait_status.py
    _wait_status = None  # type: ignore[assignment]

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


def arm_wait_status(handler, *, interval_seconds: float | None = None,
                    clock=time.monotonic):
    """A reporter that writes GPU-admission wait lines to ``handler``'s early SSE."""
    if _wait_status is None:
        return None
    early = getattr(handler, "_early_stream", None)
    if early is None or early.client_gone:
        return None
    interval = (
        _wait_status.DEFAULT_INTERVAL_SECONDS
        if interval_seconds is None else interval_seconds
    )

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

    return _wait_status.AdmissionWaitReporter(
        emit, interval_seconds=interval, clock=clock)


@contextlib.contextmanager
def armed_for(handler, chunk, *, hold_code: bool):
    """Arm a live stream for the enclosed turn when the response is early SSE."""
    wait_ctx = (
        _wait_status.armed(arm_wait_status(handler))
        if _wait_status is not None else contextlib.nullcontext()
    )
    with stream_sink.armed(arm(handler, chunk, hold_code=hold_code)) as live, wait_ctx:
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


def write_stream_body(handler, chunk, iid, model, content, elapsed_ms, receipt,
                      usage, activity, lock=None):
    """Write the answer and terminal frames of a streamed turn; ``True`` when sent."""
    live = getattr(handler, "_live_stream", None) if lock is not None else None
    with lock if lock is not None else contextlib.nullcontext():
        if live is None or not live.forwarded:
            handler.wfile.write(chunk(iid, model, {"role": "assistant", "content": content}).encode("utf-8"))
        else:
            # One completion, one id: continue under the id the deltas used.
            iid = response_id(handler, iid)
            rest, revised = live.reconcile(content)
            if revised:
                _logger.warning("streamed answer was revised after generation; sent the final version")
            if rest:
                handler.wfile.write(chunk(iid, model, {"content": rest}).encode("utf-8"))
            ttft = live.ttft_ms()
            receipt = dict(receipt or {})
            receipt["live_stream"] = {
                "revised": revised,
                **({"ttft_ms": int(ttft)} if ttft is not None else {}),
            }
        handler.wfile.write(chunk(
            iid, model, {}, finish_reason="stop", elapsed_ms=elapsed_ms,
            receipt=receipt, activity=activity,
        ).encode("utf-8"))
        if usage is not None:
            handler.wfile.write(chunk(iid, model, {}, usage=usage).encode("utf-8"))
        handler.wfile.write(b"data: [DONE]\n\n")
    return True


__all__ = [
    "CODE_FENCE", "arm", "arm_wait_status", "armed_for", "response_id",
    "wait_status_frame", "write_stream_body",
]
