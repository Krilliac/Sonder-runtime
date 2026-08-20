"""Deterministic emergency compaction for context-overflow retries."""
from __future__ import annotations

from sonder_runtime.domain.context.overflow import normalize

# --- bounded compaction -----------------------------------------------------
#
# The retry that follows a positive classification reuses the runtime's existing
# compaction discipline rather than inventing a second one: keep the system
# preamble and the newest turns, drop the oldest ones, and say so in-band. That
# is the same shape `server._session_history_messages` already applies to a
# session's live-turn window (older turns are folded away, a short system note
# stands in for them). No model call is made here, so nothing about this path can
# grow unbounded, and no message content is rewritten - messages are kept whole
# or dropped whole.

# Wording of the in-band note. It states only what happened; it never stands in
# for the dropped content, because inventing a summary without a model call
# would be fabrication.
COMPACTION_NOTE = (
    "Earlier turns in this conversation were dropped to fit the model's "
    "context window. Answer from the turns that remain."
)

_COMPACTION_NOTE_MARK = "were dropped to fit the model s context window"


def _is_system(message) -> bool:
    return isinstance(message, dict) and str(message.get("role", "")).strip().casefold() == "system"


def _role(message) -> str:
    if not isinstance(message, dict):
        return ""
    return str(message.get("role", "")).strip().casefold()


def compact_messages(messages, *, keep_recent: int = 0):
    """Drop the oldest droppable turns from a chat message list.

    Returns a new list, or ``None`` when compaction would change nothing - which
    is the signal not to retry. Nothing is dropped from the leading system
    preamble (it carries the instructions) or from the final message (it carries
    the actual request), and no message body is truncated: a request that is one
    oversized user turn cannot be made to fit without silently corrupting it, so
    it is reported as uncompactable instead.

    ``keep_recent`` optionally pins a minimum number of trailing history messages
    to preserve; the default keeps half of them.
    """
    if not isinstance(messages, (list, tuple)):
        return None
    items = [m for m in messages]
    if len(items) < 3:
        return None

    head = 0
    while head < len(items) and _is_system(items[head]):
        head += 1
    prefix = items[:head]
    # The final message is the live request and is always preserved.
    body = items[head:-1]
    tail = items[-1:]
    if len(body) < 2:
        return None

    # Never re-compact a payload that already carries the note: the retry budget
    # is exactly one, and a second pass would start eating real turns.
    for message in prefix:
        content = message.get("content") if isinstance(message, dict) else None
        folded, _ = normalize(content)
        if _COMPACTION_NOTE_MARK in folded:
            return None

    try:
        minimum_keep = max(0, int(keep_recent or 0))
    except (TypeError, ValueError, OverflowError):
        return None

    # Cut only immediately before a user message, which is the start of a
    # complete conversation turn. Cutting an arbitrary message can retain a
    # tool result without its assistant tool call (or an assistant response
    # without its user request), producing an invalid retry payload. If no
    # complete historical turn can be retained, dropping all history is safer
    # than keeping an orphaned fragment.
    target_drop = max(1, len(body) // 2)
    boundaries = [
        index for index in range(1, len(body))
        if _role(body[index]) == "user" and len(body) - index >= minimum_keep
    ]
    at_or_after = [index for index in boundaries if index >= target_drop]
    if at_or_after:
        dropped = at_or_after[0]
    elif boundaries:
        dropped = boundaries[-1]
    elif minimum_keep == 0:
        dropped = len(body)
    else:
        return None
    if dropped < 1:
        return None

    note = {"role": "system", "content": COMPACTION_NOTE}
    return list(prefix) + [note] + list(body[dropped:]) + list(tail)


def compact_overflow_payload(payload, verdict):
    """Return one compacted model payload for a classified overflow.

    This is the payload-level policy used by the model gateway.  It deliberately
    carries the original options through unchanged: retrying after compaction
    must not silently widen the context window or alter generation settings.
    ``None`` means that the payload is not eligible for a safe retry.
    """
    if not getattr(verdict, "overflow", False) or not isinstance(payload, dict):
        return None
    compacted = compact_messages(payload.get("messages"))
    if compacted is None:
        return None
    updated = dict(payload)
    updated["messages"] = compacted
    return updated
