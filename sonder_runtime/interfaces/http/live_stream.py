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
import uuid

from sonder_runtime.application.chat import stream_sink

_logger = logging.getLogger("sonder.serve")
# The chat code gate verifies fenced code after generation; code is held
# back until then instead of streamed (see stream_sink).
CODE_FENCE = "```"


def arm(handler, chunk, *, hold_code: bool) -> stream_sink.LiveTurnStream | None:
    """A live stream bound to ``handler``'s early SSE response, or ``None``."""
    handler._live_stream = None
    early = getattr(handler, "_early_stream", None)
    if early is None or early.client_gone:
        return None
    model = getattr(handler, "_stream_model", None) or "sonder"
    iid = uuid.uuid4().hex[:12]
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


@contextlib.contextmanager
def armed_for(handler, chunk, *, hold_code: bool):
    """Arm a live stream for the enclosed turn when the response is early SSE."""
    with stream_sink.armed(arm(handler, chunk, hold_code=hold_code)) as live:
        yield live


def write_stream_body(handler, chunk, iid, model, content, elapsed_ms, receipt,
                      usage, activity, lock=None):
    """Write the answer and terminal frames of a streamed turn; ``True`` when sent."""
    live = getattr(handler, "_live_stream", None) if lock is not None else None
    with lock if lock is not None else contextlib.nullcontext():
        if live is None or not live.forwarded:
            handler.wfile.write(chunk(iid, model, {"role": "assistant", "content": content}).encode("utf-8"))
        else:
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


__all__ = ["CODE_FENCE", "arm", "armed_for", "write_stream_body"]
