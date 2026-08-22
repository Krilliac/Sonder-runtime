"""Human-readable formatting for persisted checklist records."""
from __future__ import annotations


def _format_checklist(data: dict) -> str:
    symbols = {"done": "[x]", "in_progress": "[~]", "blocked": "[!]", "canceled": "[-]"}
    lines = [
        "sonder checklist %s" % data.get("id", "")[:8],
        "  %s [%s] %s" % (
            data.get("title", ""), data.get("status", "pending"),
            data.get("summary", "0/0 complete"),
        ),
    ]
    for index, item in enumerate(data.get("items") or [], 1):
        lines.append("  %s %d. %s  (%s)" % (
            symbols.get(item.get("status"), "[ ]"), index,
            item.get("title", ""), item.get("id", "")[:8],
        ))
    if not data.get("items"):
        lines.append("  (no checklist items)")
    return "\n".join(lines)


def _format_task(row: dict) -> str:
    if not row:
        return "(no task)"
    detail = (" - " + row.get("detail", "")) if row.get("detail") else ""
    scope = []
    if row.get("project"):
        scope.append("project=%s" % row["project"])
    if row.get("owner"):
        scope.append("owner=%s" % row["owner"])
    suffix = (" [" + ", ".join(scope) + "]") if scope else ""
    return "%s  p%s  %-11s %s%s%s" % (
        row.get("id", "")[:8],
        row.get("priority", 2),
        row.get("status", "pending"),
        row.get("title", ""),
        detail,
        suffix,
    )
