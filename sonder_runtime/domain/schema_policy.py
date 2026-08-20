"""Pure presentation policy for reporting schema-verification gaps."""

from __future__ import annotations


MAX_REPORTED_SCHEMA_GAPS = 8


def format_schema_gaps(gaps) -> str:
    """Render bounded schema-verification gaps for human and machine callers."""
    gaps = list(gaps)
    shown = [
        "%s (%s)" % (path, reason)
        for path, reason in gaps[:MAX_REPORTED_SCHEMA_GAPS]
    ]
    remaining = len(gaps) - len(shown)
    if remaining > 0:
        shown.append("and %d more" % remaining)
    return "; ".join(shown)
