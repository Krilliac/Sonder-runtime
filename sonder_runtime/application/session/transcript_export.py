"""Redacted plain-text transcript rendering for the ``session_export`` tool.

The legacy memory store keeps ``interactions.task`` / ``interactions.response``
verbatim, so a remembered turn such as ``password=hunter2`` or a pasted
``sk-...`` key would come back unchanged from a transcript export.  The
durable HTTP export already scrubs those shapes: events are captured through
:func:`sonder_runtime.domain.security.redaction.redact_text` and exported
through :class:`.query_export.DefaultExportRedactor`.  This module applies that
same composition to the text transcript instead of inventing a second policy.

Each field is redacted on its own before the lines are joined, so a multi-line
pattern (an ``Authorization:`` value, a PEM block) can never reach across a
``USER:`` / ``ASSISTANT:`` boundary and swallow transcript structure.  Text
with no secret shape passes through unchanged, keeping the output
byte-identical to the unredacted rendering.

Redaction is fail-closed: if the redactor raises or returns a non-string, the
field is replaced with ``[REDACTION_FAILED]`` rather than emitted raw.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ...domain.security import redaction as _redaction
from .query_export import DefaultExportRedactor

_EXPORT_REDACTOR = DefaultExportRedactor()


def redact_export_text(text: Any) -> str:
    """Scrub one transcript field with the durable-export redaction policy."""
    value = "" if text is None else str(text)
    try:
        out = _EXPORT_REDACTOR.redact(_redaction.redact_text(value))
    except Exception:
        return _redaction.REDACTION_FAILED
    return out if isinstance(out, str) else _redaction.REDACTION_FAILED


def format_session_transcript(
    session_id: str,
    session: Mapping[str, Any],
    turns: Iterable[Mapping[str, Any]],
) -> str:
    """Render a stored session as ``session_export`` text, secrets redacted."""
    lines = [
        "session: %s" % session_id,
        "title: %s" % (redact_export_text(session.get("title")) or "(untitled)"),
        "project: %s" % (redact_export_text(session.get("project")) or "(none)"),
        "",
    ]
    for turn in turns:
        lines.append("USER: %s" % redact_export_text(turn.get("task")))
        lines.append("ASSISTANT: %s" % redact_export_text(turn.get("response")))
        lines.append("")
    return "\n".join(lines).rstrip()


__all__ = ["format_session_transcript", "redact_export_text"]
