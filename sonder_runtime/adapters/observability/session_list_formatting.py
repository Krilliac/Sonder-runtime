"""Pure presentation for the /sessions thread listing.

Input is the ``memory_store.list_sessions`` row shape (most recent first,
``{session_id, title, updated_ts, project, turn_count}``).  The id stays
first on each line because it is what ``/resume`` and ``/replay`` accept.
Plain text, no ANSI, like every formatter in this package.
"""
from __future__ import annotations

import calendar
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import TypedDict

    class SessionRow(TypedDict, total=False):
        session_id: str | None
        title: str | None
        updated_ts: str | None
        project: str | None
        turn_count: int | None


def relative_age(updated_ts: str | None, *, now: float | None = None) -> str:
    """Render a SQLite ``CURRENT_TIMESTAMP`` value as a short relative age.

    ``CURRENT_TIMESTAMP`` is UTC ``YYYY-MM-DD HH:MM:SS``.  Any other shape is
    returned unchanged: showing the stored value beats fabricating an age
    from a format this parser does not actually understand.
    """
    value = str(updated_ts or "").strip()
    if not value:
        return ""
    try:
        then = calendar.timegm(time.strptime(value, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return value
    current = time.time() if now is None else float(now)
    delta = int(current - then)
    if delta < 0:
        # A stored timestamp ahead of this clock is a skew artifact, not a
        # future session; "now" is the honest bound.
        return "now"
    if delta < 60:
        return "%ds ago" % delta
    if delta < 3600:
        return "%dm ago" % (delta // 60)
    if delta < 86400:
        return "%dh ago" % (delta // 3600)
    return "%dd ago" % (delta // 86400)


def format_sessions(
    rows: Iterable[SessionRow] | None, *, now: float | None = None
) -> str:
    """Render the session listing, or the stable empty-state line."""
    rows = list(rows or [])
    if not rows:
        return "(no past sessions)"
    lines = []
    for row in rows:
        row = row if isinstance(row, dict) else {}
        age = relative_age(row.get("updated_ts"), now=now)
        project = str(row.get("project") or "").strip()
        parts = [
            "  %s" % row.get("session_id"),
            "[%d turns]" % int(row.get("turn_count") or 0),
        ]
        if age:
            parts.append(age)
        parts.append(str(row.get("title") or "(untitled)"))
        if project:
            parts.append("· %s" % project)
        lines.append("  ".join(parts))
    return "\n".join(lines)


__all__ = ["format_sessions", "relative_age"]
