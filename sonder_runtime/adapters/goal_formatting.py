"""Human-readable formatting for goal records."""
from __future__ import annotations


def format_goal(goal: dict | None) -> str:
    """Render one goal record for the user-facing ``/goal`` command."""
    if not goal:
        return "no active goal"
    lines = ["%s [%s] %s" % (
        goal["id"], goal["status"], goal["objective"],
    )]
    for criterion in goal.get("criteria") or []:
        lines.append("  criterion: %s" % criterion)
    for note in (goal.get("notes") or [])[-5:]:
        lines.append("  note: %s" % note.get("text", ""))
    return "\n".join(lines)
