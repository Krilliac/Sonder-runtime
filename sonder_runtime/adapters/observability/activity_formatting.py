"""Operator-facing rendering of authorized activity snapshots."""
from __future__ import annotations

import sonder_runtime.adapters.observability.activity_tracker as activity_tracker


def _format_activity_status(source: dict, include_events: bool = True, *, scope="") -> str:
    snap = activity_tracker.public_snapshot(source)
    if snap is None:
        return "sonder activity\n  state: unknown"
    lines = [
        "sonder activity%s" % (" (%s)" % scope if scope else ""),
        "  active responses: %s" % snap.get("active_count", 0),
        "  total tool calls since start: %s" % snap.get("total_tool_calls", 0),
    ]
    active = snap.get("active") or []
    if active:
        lines.append("  active:")
        for row in active[-8:]:
            last = row.get("last_event") or {}
            lines.append(
                "    %s %s tools=%s models=%s tokens=%s/%s last=%s" % (
                    row.get("id"),
                    row.get("label"),
                    row.get("tool_calls", 0),
                    row.get("model_calls", 0),
                    row.get("tokens_in", 0),
                    row.get("tokens_out", 0),
                    last.get("kind", "starting"),
                )
            )
    latest = snap.get("latest")
    if latest:
        lines.extend(["", activity_tracker.format_response(latest)])
    elif include_events:
        lines.append("  latest: (none yet)")
    if include_events:
        lines.extend([
            "",
            activity_tracker.format_execution_feed(
                activity_tracker.execution_feed(source)
            ),
        ])
    return "\n".join(lines)
