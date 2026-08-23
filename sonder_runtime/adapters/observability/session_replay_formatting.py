"""Pure presentation for re-rendering a stored conversation thread.

``/sessions`` and ``/resume`` let an operator find and continue an earlier
thread, but the only way to *see* one was to resume it and scroll memory.
This module renders a bounded, read-only transcript of stored turns so
``/replay`` can show what a thread contains before (or without) resuming.

Presentation only: input is the ``memory_store.session_turns`` row shape
(``{id, task, response}`` oldest-first), output is plain text with no ANSI,
matching every other formatter in this package.  Stored responses keep their
durable interaction footer and any trace/activity blocks; a replay strips
that chrome the same way the live console did when it first printed them.
"""
from __future__ import annotations

from sonder_runtime.adapters.observability import response_formatting

REPLAY_DEFAULT_TURNS = 20
REPLAY_MAX_TURNS = 200
# Per-field bound so one giant pasted blob cannot flood the terminal; replay
# is a reading aid, not an export (memory_export / session_export own that).
REPLAY_TEXT_LIMIT = 4000

# One copy of the wording, matching trace_buffer._format_trace exactly.
_TRACE_MARKER = "=== TRACE (how Sonder Runtime decided) ==="


def clean_response(text) -> str:
    """Strip the durable footer, trace block, and activity block for display."""
    value = str(text or "")
    idx = value.find(response_formatting.FOOTER_PREFIX)
    if idx != -1:
        value = value[:idx]
    idx = value.find(_TRACE_MARKER)
    if idx != -1:
        value = value[:idx]
    # Same activity-block removal the response footer path already applies;
    # replay must not re-print evidence chrome as if it were answer text.
    return response_formatting._strip_activity_block(value).rstrip()


def _bounded(text) -> str:
    value = str(text or "").strip()
    if len(value) > REPLAY_TEXT_LIMIT:
        omitted = len(value) - REPLAY_TEXT_LIMIT
        value = value[:REPLAY_TEXT_LIMIT].rstrip()
        value += "\n... (+%d more characters)" % omitted
    return value


def _block(prefix: str, text: str) -> str:
    """Prefix the first line; hang-indent continuation lines to align."""
    lines = (text or "").splitlines() or [""]
    pad = " " * len(prefix)
    return "\n".join(
        (prefix if index == 0 else pad) + line
        for index, line in enumerate(lines)
    )


def clamp_turn_limit(value) -> int:
    """Coerce a requested turn count into the supported replay window."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return REPLAY_DEFAULT_TURNS
    return max(1, min(REPLAY_MAX_TURNS, count))


def format_session_replay(turns, *, session_id="", limit=REPLAY_DEFAULT_TURNS) -> str:
    """Render stored turns oldest-first, bounded to the last ``limit``."""
    rows = list(turns or [])
    total = len(rows)
    limit = clamp_turn_limit(limit)
    shown = rows[-limit:]
    header = "replay %s  ·  %d turn(s)" % (
        str(session_id or "(current thread)"), total,
    )
    if len(shown) < total:
        header += "  ·  showing last %d (/replay <id> %d shows more)" % (
            len(shown), min(REPLAY_MAX_TURNS, total),
        )
    if not rows:
        return header + "\n  (no stored turns in this thread yet)"
    lines = [header]
    first_shown = total - len(shown) + 1
    for offset, row in enumerate(shown):
        number = first_shown + offset
        task = _bounded(row.get("task") if isinstance(row, dict) else "")
        response = _bounded(
            clean_response(row.get("response") if isinstance(row, dict) else "")
        )
        lines.append("")
        lines.append(_block("[%d] you    | " % number, task or "(empty)"))
        lines.append(_block("    sonder | ", response or "(empty response)"))
    return "\n".join(lines)


__all__ = [
    "REPLAY_DEFAULT_TURNS",
    "REPLAY_MAX_TURNS",
    "REPLAY_TEXT_LIMIT",
    "clamp_turn_limit",
    "clean_response",
    "format_session_replay",
]
