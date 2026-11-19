"""Human-readable formatting for persisted checklist records."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TypedDict

    class _TaskRecord(TypedDict, total=False):
        id: str
        title: str
        detail: str
        status: str
        priority: int
        project: str
        owner: str
        parent_id: str

    class _ChecklistRecord(TypedDict, total=False):
        id: str
        title: str
        status: str
        project: str
        owner: str
        items: Sequence[_TaskRecord]
        summary: str


def _format_checklist(data: _ChecklistRecord) -> str:
    symbols: dict[str | None, str] = {
        "done": "[x]", "in_progress": "[~]", "blocked": "[!]", "canceled": "[-]",
    }
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


def _format_task(row: _TaskRecord) -> str:
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
