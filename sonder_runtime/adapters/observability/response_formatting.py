"""Response footer and observable-activity formatting for runtime interfaces."""
from __future__ import annotations

import re

import sonder_runtime.adapters.observability.activity_tracker as activity_tracker

FOOTER_PREFIX = "\n\n[interaction_id: "
_FOOTER_RE = re.compile(r"\[interaction_id: ([0-9a-f]+)\]\s*$")


def with_footer(text, interaction_id):
    current = activity_tracker.current()
    activity = activity_tracker.format_response(current) if current else ""
    if (
        activity
        and not activity.startswith("activity:")
        and "=== ACTIVITY (observable work) ===" not in (text or "")
    ):
        text = "%s\n\n%s" % (text, activity)
    return "%s%s%s]" % (text, FOOTER_PREFIX, interaction_id)


def _strip_activity_block(text):
    """Remove the final observable-activity block while preserving other text."""
    value = str(text or "")
    marker = "=== ACTIVITY (observable work) ==="
    end_marker = "=== END ACTIVITY ==="
    start = value.rfind(marker)
    if start < 0:
        return value
    end = value.find(end_marker, start)
    if end < 0:
        return value
    end += len(end_marker)
    before = value[:start].rstrip()
    after = value[end:].lstrip()
    return "\n\n".join(part for part in (before, after) if part)


def _append_activity(text, response=None, replace=False):
    current = response if response is not None else activity_tracker.current()
    if replace:
        text = _strip_activity_block(text)
    activity = activity_tracker.format_response(current) if current else ""
    if (
        activity
        and not activity.startswith("activity:")
        and "=== ACTIVITY (observable work) ===" not in (text or "")
    ):
        footer = _FOOTER_RE.search(text or "")
        if footer:
            before = (text or "")[:footer.start()].rstrip()
            return "%s\n\n%s\n\n%s" % (
                before, activity, (text or "")[footer.start():],
            )
        return "%s\n\n%s" % (text, activity)
    return text


def parse_interaction_id(text):
    match = _FOOTER_RE.search(text or "")
    return match.group(1) if match else None
