"""Human-readable formatting policies for runtime health snapshots."""
from __future__ import annotations


def format_context_health(data: dict) -> str:
    """Render a context-health snapshot without owning its data collection."""
    lines = [
        "sonder context health",
        "  status: %s" % data.get("status", "unknown"),
        "  session: %s%s" % (
            data.get("session", "none"),
            " (%s)" % data.get("title") if data.get("title") else "",
        ),
        "  context %s %s%%  ~%s/%s tokens" % (
            data.get("context_bar", ""),
            data.get("context_percent", 0),
            data.get("estimated_tokens", 0),
            data.get("context_limit", 0),
        ),
        "  native  ~%s token Ollama num_ctx (%s mode)" % (
            data.get("native_context_limit", 0),
            data.get("context_mode", "native"),
        ),
        "  live    %s %s/%s turns in active prompt (%s total)" % (
            data.get("turn_bar", ""),
            data.get("live_turns", 0),
            data.get("max_live_turns", 0),
            data.get("total_turns", 0),
        ),
        "  memory  %s %s lessons, %s facts, %s prefs, %s interactions, %s outcomes" % (
            data.get("memory_bar", ""),
            data.get("lessons", 0),
            data.get("facts", 0),
            data.get("preferences", 0),
            data.get("interactions", 0),
            data.get("outcomes", 0),
        ),
        "  summary: %s chars, ~%s tokens%s" % (
            data.get("summary_chars", 0),
            data.get("summary_tokens", 0),
            " through %s" % data.get("summarized_through")
            if data.get("summarized_through") else "",
        ),
        "  db: %s" % data.get("db_path", ""),
    ]
    return "\n".join(lines)


__all__ = ["format_context_health"]
