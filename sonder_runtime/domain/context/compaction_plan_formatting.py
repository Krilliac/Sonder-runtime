"""Pure operator-facing rendering of a context compaction plan.

The plan is assembled from live context health elsewhere; this module only
turns it into the bounded text block the compaction-plan surfaces print.
Moved from ``server.py`` in the WP1 Three-Hundred-Seventeenth Slice with its
behaviour byte-for-byte intact.
"""
from __future__ import annotations


def format_context_compaction_plan(plan: dict) -> str:
    ctx = plan.get("context", {})
    lines = [
        "sonder context compaction plan",
        "  session: %s" % ctx.get("session", "none"),
        "  context: %s%%  ~%s/%s tokens (%s mode)" % (
            ctx.get("context_percent", 0),
            ctx.get("estimated_tokens", 0),
            ctx.get("context_limit", 0),
            ctx.get("context_mode", "native"),
        ),
        "  live turns: %s/%s | summary: ~%s tokens" % (
            ctx.get("live_turns", 0),
            ctx.get("max_live_turns", 0),
            ctx.get("summary_tokens", 0),
        ),
        "  recommended actions:",
    ]
    for item in plan.get("actions", []):
        lines.append("    [%s] %s" % (item.get("priority", "info"), item.get("action", "")))
        lines.append("        -> %s" % item.get("reason", ""))
    return "\n".join(lines)
