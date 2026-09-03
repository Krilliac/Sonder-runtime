"""Pure operator-facing rendering of the improvement report.

The report data is assembled elsewhere from learning, memory, context, MCP
and autopilot health; this module only turns that dictionary into the
bounded text block the ``/improve`` surfaces print. It is explicit-input and
side-effect free. Moved from ``server.py`` in the WP1 Two-Hundred-Ninety-Eighth
Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations


def format_improvement_report(report: dict) -> str:
    acceptance = report.get("acceptance_percent")
    unknown_outcomes = max(0, int(report.get("unknown_source_outcomes", 0) or 0))
    caller_judged = (
        "unmeasured" if acceptance is None else "%s%% of %s reviewed" % (
            acceptance, report.get("reviewed_outcomes", 0),
        )
    )
    lines = [
        "sonder improvement report",
        "  readiness score: %s/100" % report.get("score", 0),
        "  learning: %s interactions, %s outcomes, %s%% covered" % (
            report.get("interactions", 0),
            report.get("outcomes", 0),
            report.get("learning_health", {}).get("outcome_coverage_percent", 0),
        ),
        # Never show the blended rate alone: it is dominated by the runtime
        # marking its own curriculum, and reads as a quality score when it
        # is not one.
        "    caller-judged: %s | autograded: %s%% of %s | "
        "legacy/unknown provenance: %s | blended: %s%%" % (
            caller_judged,
            report.get("learning_health", {}).get("autograded_positive_percent", 0),
            report.get("autograded_outcomes", 0),
            unknown_outcomes,
            report.get("learning_health", {}).get("positive_percent", 0),
        ),
        "  memory: %s lessons, %s facts, duplicate rows=%s, vague=%s, missing embeddings=%s" % (
            report.get("lessons", 0),
            report.get("facts", 0),
            report.get("memory_quality", {}).get("duplicates", 0),
            report.get("memory_quality", {}).get("vague", 0),
            report.get("memory_quality", {}).get("no_embedding", 0),
        ),
        "  context: %s | hosted/cloud: %s" % (
            report.get("context_status", "unknown"),
            "enabled" if report.get("cloud_allowed") else "disabled",
        ),
        (
            "  autonomy: unavailable"
            if not report.get("autopilot", {}).get("available", True)
            else "  autonomy: %s active | %s resumable" % (
                report.get("autopilot", {}).get("active", 0),
                report.get("autopilot", {}).get("resumable", 0),
            )
        ),
        "  mcp: %s | %s tools | %s atomic refreshes" % (
            report.get("mcp_runtime", {}).get("status", "unknown"),
            report.get("mcp_runtime", {}).get("registered_tools", 0),
            report.get("mcp_runtime", {}).get("refresh_count", 0),
        ),
        "  next improvements:",
    ]
    for issue in report.get("issues", [])[:8]:
        lines.append("    [%s] %s: %s" % (
            issue.get("severity", "info"),
            issue.get("area", "system"),
            issue.get("title", ""),
        ))
        lines.append("        -> %s" % issue.get("action", ""))
    return "\n".join(lines)
