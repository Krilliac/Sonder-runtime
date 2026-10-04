"""Deterministic emergency compaction for context-overflow retries."""
from __future__ import annotations

import json

# --- bounded compaction -----------------------------------------------------
#
# The retry that follows a positive classification reuses the runtime's existing
# compaction discipline: keep the system preamble and newest turns, and combine
# older plain-text turns with their source roles. Without an archive authority
# here we cannot discard tool evidence or infer which prose is disposable.

COMPACTION_NOTE = "Prior [role, content]:"
MAX_OVERFLOW_MESSAGES = 10_000
MAX_RETAINED_HISTORY_BYTES = 256 * 1024


def _is_system(message) -> bool:
    return isinstance(message, dict) and str(message.get("role", "")).strip().casefold() == "system"


def _role(message) -> str:
    if not isinstance(message, dict):
        return ""
    return str(message.get("role", "")).strip().casefold()


def compact_messages(messages, *, keep_recent: int = 0):
    """Coalesce older plain-text turns without losing their words or roles.

    Returns a new list, or ``None`` when compaction would change nothing - which
    is the signal not to retry. Nothing is dropped from the leading system
    preamble (it carries the instructions) or from the final message (it carries
    the actual request), and no message body is truncated: a request that is one
    oversized user turn cannot be made to fit without silently corrupting it, so
    it is reported as uncompactable instead.

    ``keep_recent`` optionally pins a minimum number of trailing history messages
    to preserve; the default keeps half of them.
    """
    if not isinstance(messages, (list, tuple)) or len(messages) > MAX_OVERFLOW_MESSAGES:
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
    # is exactly one, and another pass would add unnecessary nesting.
    for message in items:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.startswith(COMPACTION_NOTE):
            return None

    try:
        minimum_keep = max(0, int(keep_recent or 0))
    except (TypeError, ValueError, OverflowError):
        return None

    # Cut only immediately before a user message, which is the start of a
    # complete conversation turn. Cutting an arbitrary message can retain a
    # tool result without its assistant tool call (or an assistant response
    # without its user request), producing an invalid retry payload. If no
    # complete historical turn can be retained, coalesce all eligible history
    # rather than split a protocol group.
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

    earlier = body[:dropped]
    # Conversation prose can contain an accepted decision, constraint or
    # failure without any structured tag. Preserve it all. Tool calls and
    # multimodal content need their original protocol shape, so without a
    # verified archive we refuse to compact those older turns.
    if any(
        not isinstance(message, dict)
        or set(message) != {"role", "content"}
        or _role(message) not in {"user", "assistant"}
        or not isinstance(message.get("content"), str)
        for message in earlier
    ):
        return None
    retained = json.dumps(
        [[message["role"], message["content"]] for message in earlier],
        ensure_ascii=False, separators=(",", ":"),
    )
    if len(retained.encode("utf-8")) > MAX_RETAINED_HISTORY_BYTES:
        return None
    note = {"role": "user", "content": COMPACTION_NOTE + "\n" + retained}
    updated = list(prefix) + [note] + list(body[dropped:]) + list(tail)
    try:
        original_bytes = len(json.dumps(items, ensure_ascii=False).encode("utf-8"))
        updated_bytes = len(json.dumps(updated, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return None
    # A no-loss projection that would grow the request earns no retry.
    return updated if updated_bytes < original_bytes else None


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
