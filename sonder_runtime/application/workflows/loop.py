"""Pure bounded loop execution used by saved and inline workflows."""
from __future__ import annotations

import time

DEFAULT_LOOP_ITERATIONS = 5
MAX_LOOP_ITERATIONS = 50
MAX_LOOP_DELAY_SECONDS = 10.0
MAX_LOOP_ACTIONS = 100


def clamp_iterations(max_iterations):
    try:
        value = int(max_iterations)
    except (TypeError, ValueError, OverflowError):
        value = DEFAULT_LOOP_ITERATIONS
    return max(1, min(value, MAX_LOOP_ITERATIONS))


def clamp_delay(delay_seconds):
    try:
        value = float(delay_seconds)
    except (TypeError, ValueError):
        value = 0.0
    return max(0.0, min(value, MAX_LOOP_DELAY_SECONDS))


def _cancel_requested(cancel_check):
    if cancel_check is None:
        return False
    try:
        return bool(cancel_check())
    except Exception:
        # Cancellation is a safety gate. A broken checker must not authorize
        # another action or another loop iteration.
        return True


def _wait(delay_seconds, cancel_check):
    deadline = time.monotonic() + delay_seconds
    while True:
        if _cancel_requested(cancel_check):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(remaining, 0.1))


def run_loop(
    actions,
    dispatch_action,
    max_iterations=DEFAULT_LOOP_ITERATIONS,
    stop_on_failure=True,
    stop_on_success=False,
    delay_seconds=0,
    cancel_check=None,
):
    if not isinstance(actions, list) or not actions:
        raise ValueError("actions must be a non-empty JSON list")
    if len(actions) > MAX_LOOP_ACTIONS:
        raise ValueError("actions exceed the %d-action loop limit" % MAX_LOOP_ACTIONS)
    max_iterations = clamp_iterations(max_iterations)
    delay_seconds = clamp_delay(delay_seconds)
    iterations = []
    stop_reason = "max_iterations reached"
    cancelled = False
    for iteration in range(1, max_iterations + 1):
        if _cancel_requested(cancel_check):
            cancelled = True
            stop_reason = "cancelled before iteration %d" % iteration
            break
        action_rows = []
        iteration_ok = True
        failed_index = None
        for index, action in enumerate(actions, start=1):
            if _cancel_requested(cancel_check):
                cancelled = True
                iteration_ok = False
                stop_reason = "cancelled before action %d in iteration %d" % (
                    index, iteration,
                )
                break
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
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                result = {
                    "ok": False,
                    "type": action.get("type", "(unknown)"),
                    "summary": "dispatcher returned an invalid result",
                    "output": repr(result),
                }
            action_rows.append({"index": index, "result": result})
            if _cancel_requested(cancel_check):
                cancelled = True
                iteration_ok = False
                stop_reason = "cancelled after action %d in iteration %d" % (
                    index, iteration,
                )
                break
            if not result["ok"]:
                iteration_ok = False
                failed_index = index
            if failed_index is not None and stop_on_failure:
                break
        iterations.append(
            {"iteration": iteration, "ok": iteration_ok, "actions": action_rows}
        )
        if cancelled:
            break
        if stop_on_failure and not iteration_ok:
            stop_reason = "action %d failed in iteration %d" % (
                failed_index, iteration,
            )
            break
        if stop_on_success and iteration_ok:
            stop_reason = "iteration %d succeeded" % iteration
            break
        if iteration < max_iterations and delay_seconds:
            if _wait(delay_seconds, cancel_check):
                cancelled = True
                stop_reason = "cancelled after iteration %d" % iteration
                break
    return {
        "ok": not cancelled and (iterations[-1]["ok"] if iterations else False),
        "cancelled": cancelled,
        "iterations": iterations,
        "stop_reason": stop_reason,
        "max_iterations": max_iterations,
        "delay_seconds": delay_seconds,
    }


def _trim_output(text, limit=3000):
    text = str(text or "")
    return text if len(text) <= limit else text[-limit:]


def format_loop_result(loop_result):
    iterations = loop_result.get("iterations") or []
    lines = [
        "loop status: %s" % (
            "cancelled" if loop_result.get("cancelled")
            else ("ok" if loop_result.get("ok") else "failed")
        ),
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
