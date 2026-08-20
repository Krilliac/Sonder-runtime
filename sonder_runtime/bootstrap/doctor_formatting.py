"""Pure presentation and rollup policy for the packaged doctor boundary."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sonder_runtime.domain.doctor_status import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_WARN,
)


_SEVERITY = {STATUS_OK: 0, STATUS_SKIPPED: 0, STATUS_WARN: 1, STATUS_FAIL: 2}
_SEVERITY_TO_STATUS = {0: STATUS_OK, 1: STATUS_WARN, 2: STATUS_FAIL}


def rollup_status(entries: Iterable[Mapping[str, Any]]) -> str:
    """Return the worst status in normalized doctor result entries."""
    worst = 0
    for entry in entries:
        worst = max(worst, _SEVERITY[entry["status"]])
    return _SEVERITY_TO_STATUS[worst]


def render_report(report: Mapping[str, Any]) -> str:
    """Render a doctor's structured report as a stable terminal summary."""
    overall = str(report.get("overall", "unknown")).lower()
    checks = list(report.get("checks") or [])

    lines = ["sonder doctor: %s" % overall.upper()]
    if not checks:
        lines.append("  (no checks run)")
        return "\n".join(lines)

    marks = {
        STATUS_OK: "ok",
        STATUS_WARN: "warn",
        STATUS_FAIL: "FAIL",
        STATUS_SKIPPED: "skip",
    }
    name_width = max(len(str(c.get("name", ""))) for c in checks)
    for check in checks:
        name = str(check.get("name", ""))
        status = str(check.get("status", "")).lower()
        mark = marks.get(status, status or "?")
        detail = str(check.get("detail", "") or "")
        line = "  [%-4s] %-*s" % (mark, name_width, name)
        if detail:
            line = "%s  %s" % (line, detail)
        lines.append(line.rstrip())
    return "\n".join(lines)


__all__ = [
    "STATUS_FAIL",
    "STATUS_OK",
    "STATUS_SKIPPED",
    "STATUS_WARN",
    "render_report",
    "rollup_status",
]
