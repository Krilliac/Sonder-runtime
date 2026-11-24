"""Pure, bounded alternative-method & recovery planner when a tool or command fails."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sonder_runtime.domain.resilience import plan_alternatives


def plan_fallback(
    failed_tool: str,
    *,
    error: str = "",
    goal: str = "",
    available_tools: Sequence[str] | None = None,
    tried_tools: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Classify a tool/command failure and return ranked, policy-safe alternative tools and steps."""
    if not str(failed_tool or "").strip():
        return {"ok": False, "error": "failed_tool is required"}
    return plan_alternatives(
        failed_tool=failed_tool,
        error=error,
        goal=goal,
        available_tools=available_tools,
        tried_tools=tried_tools,
    )


__all__ = ["plan_fallback"]
