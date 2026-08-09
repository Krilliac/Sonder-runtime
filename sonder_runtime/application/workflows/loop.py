"""Pure bounded loop execution used by saved and inline workflows."""
from __future__ import annotations

import time

DEFAULT_LOOP_ITERATIONS = 5
MAX_LOOP_ITERATIONS = 50
MAX_LOOP_DELAY_SECONDS = 10.0


def clamp_iterations(max_iterations):
    try:
        value = int(max_iterations)
    except (TypeError, ValueError):
        value = DEFAULT_LOOP_ITERATIONS
    return max(1, min(value, MAX_LOOP_ITERATIONS))


def clamp_delay(delay_seconds):
    try:
        value = float(delay_seconds)
    except (TypeError, ValueError):
        value = 0.0
    return max(0.0, min(value, MAX_LOOP_DELAY_SECONDS))


def run_loop(
    actions,
    dispatch_action,
    max_iterations=DEFAULT_LOOP_ITERATIONS,
    stop_on_failure=True,
    stop_on_success=False,
    delay_seconds=0,
):
    if not isinstance(actions, list) or not actions:
        raise ValueError("actions must be a non-empty JSON list")
    max_iterations = clamp_iterations(max_iterations)
    delay_seconds = clamp_delay(delay_seconds)
    iterations = []
    stop_reason = "max_iterations reached"
    for iteration in range(1, max_iterations + 1):
        action_rows = []
        iteration_ok = True
        failed_index = None
        for index, action in enumerate(actions, start=1):
            if not isinstance(action, dict):
                result = {
                    "ok": False,
                    "type": "(invalid)",
                    "summary": "action must be an object",
                    "output": repr(action),
                }
            else:
                try:
                    result = dispatch_action(action)
                except Exception as exc:
                    result = {
                        "ok": False,
                        "type": action.get("type", "(unknown)"),
                        "summary": "%s: %s" % (exc.__class__.__name__, exc),
                        "output": "",
                    }
            if not result.get("ok"):
                iteration_ok = False
                failed_index = index
            action_rows.append({"index": index, "result": result})
            if failed_index is not None and stop_on_failure:
                break
        iterations.append(
            {"iteration": iteration, "ok": iteration_ok, "actions": action_rows}
        )
        if stop_on_failure and not iteration_ok:
            stop_reason = "action %d failed in iteration %d" % (
                failed_index, iteration,
            )
            break
        if stop_on_success and iteration_ok:
            stop_reason = "iteration %d succeeded" % iteration
            break
        if iteration < max_iterations and delay_seconds:
            time.sleep(delay_seconds)
    return {
        "ok": iterations[-1]["ok"] if iterations else False,
        "iterations": iterations,
        "stop_reason": stop_reason,
        "max_iterations": max_iterations,
        "delay_seconds": delay_seconds,
    }


def _trim_output(text, limit=3000):
    text = text or ""
    return text if len(text) <= limit else text[-limit:]


def format_loop_result(loop_result):
    iterations = loop_result.get("iterations") or []
    lines = [
        "loop status: %s" % ("ok" if loop_result.get("ok") else "failed"),
        "iterations: %d/%d" % (len(iterations), loop_result.get("max_iterations")),
        "stop reason: %s" % loop_result.get("stop_reason"),
    ]
    for iteration in iterations:
        lines.append("")
        lines.append(
            "iteration %d: %s"
            % (iteration["iteration"], "ok" if iteration.get("ok") else "failed")
        )
        for row in iteration.get("actions", []):
            result = row["result"]
            action_type = result.get("type") or "(unknown)"
            status = "ok" if result.get("ok") else "failed"
            summary = result.get("summary") or ""
            lines.append(
                "  [%d] %s: %s%s"
                % (
                    row["index"], action_type, status,
                    (" - " + summary) if summary else "",
                )
            )
            output = _trim_output(result.get("output") or "")
            if output:
                for line in output.splitlines():
                    lines.append("      " + line)
    return "\n".join(lines)
