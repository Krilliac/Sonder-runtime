"""In-process diagnostic report for model-call retries, tool-use recoveries, and circuit breakers."""
from __future__ import annotations

from typing import Any

from sonder_runtime.domain.resilience import GLOBAL_CIRCUIT_BREAKER, GLOBAL_TELEMETRY


def runtime_resilience_check() -> dict[str, Any]:
    """Return bounded telemetry of model retries, context compactions, arg repairs, loop breaks, and open circuits."""
    telem = GLOBAL_TELEMETRY.snapshot()
    cb = GLOBAL_CIRCUIT_BREAKER.snapshot()
    counters = telem.get("counters") or {}
    recommendations: list[str] = []
    if counters.get("context_compaction_retry", 0) > 0:
        recommendations.append("Context overflows were auto-recovered via compaction; prefer targeted file_read_range / text_search over reading entire large files.")
    if counters.get("repeated_tool_loop_hint", 0) > 0:
        recommendations.append("Repeated identical tool calls were intercepted; call plan_fallback to switch strategies earlier.")
    if counters.get("model_call_failed", 0) > counters.get("model_call_recovered", 0):
        recommendations.append("Unrecovered model-call failures occurred; verify local inference endpoints (:11438 / :11437) with sonder-stack.ps1 -Action Status.")
    if cb.get("open_circuits"):
        recommendations.append(f"Active circuit breakers open: {', '.join(sorted(cb['open_circuits']))}.")
    return {
        "ok": True,
        "healthy": not bool(cb.get("open_circuits")),
        "telemetry": telem,
        "circuit_breaker": cb,
        "recommendations": recommendations,
    }


__all__ = ["runtime_resilience_check"]
