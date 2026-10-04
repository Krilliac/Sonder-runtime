"""ATIF rendering for the remembered-conversation ``session_export`` tool.

The ``session_export`` MCP tool's default output (readable transcript text)
is unchanged; ``format="atif"`` routes here.  Turns are read from the same
interactions rows, oldest first, with the same ``limit`` tail, and projected
by :func:`sonder_runtime.application.session.atif.interaction_turns_to_atif`,
which applies the export redaction to every string.
"""
from __future__ import annotations

import json
from collections.abc import Mapping

from ..application.session.atif import AtifAgent, interaction_turns_to_atif
from ..domain.common.errors import InvalidInput
from ..platform.version import runtime_version

SESSION_EXPORT_FORMATS = ("text", "atif")


def normalize_session_export_format(value: object) -> str | None:
    """``""``/``"text"`` -> ``"text"``, ``"atif"`` -> ``"atif"``, else None."""
    chosen = str(value or "").strip().lower() or "text"
    return chosen if chosen in SESSION_EXPORT_FORMATS else None


def _turn_records(conn, session_id: str) -> list[dict[str, object]]:
    # Same row set and ordering as memory_store.session_turns, plus the
    # recorded tier, timestamp and token counts ATIF can carry.
    rows = conn.execute(
        "SELECT id, task, response, tier, ts, tokens_in, tokens_out, token_source "
        "FROM interactions WHERE session_id=? ORDER BY ts ASC, rowid ASC",
        (session_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def interaction_session_atif(conn, session_id: str, session: Mapping[str, object],
                             *, limit: int) -> str:
    """Render one remembered session's last ``limit`` turns as ATIF JSON."""
    turns = _turn_records(conn, session_id)[-limit:]
    if not turns:
        raise InvalidInput("session has no turns to export as ATIF")
    extra: dict[str, object] = {}
    if session.get("title"):
        extra["title"] = session["title"]
    if session.get("project"):
        extra["project"] = session["project"]
    document = interaction_turns_to_atif(
        turns, session_id=session_id, agent=AtifAgent(version=runtime_version()), extra=extra,
    )
    return json.dumps(document, indent=2, ensure_ascii=False)


__all__ = ["SESSION_EXPORT_FORMATS", "interaction_session_atif", "normalize_session_export_format"]
