"""Operator-facing rendering for bounded command and tool runs."""
from __future__ import annotations

import json


def format_run_result(title: str, data: dict) -> str:
    """Render a harness result while preserving its diagnostic sections."""
    lines = [
        title,
        "  command: %s" % json.dumps(data.get("command") or [], ensure_ascii=False),
        "  cwd: %s" % data.get("cwd", ""),
        "  ok: %s" % data.get("ok", False),
        "  returncode: %s" % data.get("returncode"),
        "  timed_out: %s" % data.get("timed_out", False),
        "  elapsed_ms: %s" % data.get("elapsed_ms", 0),
    ]
    # Keep the reason before child output: infrastructure-error readers stop at
    # the stdout marker and need this field to distinguish no-run failures.
    if data.get("error"):
        lines.append("  error: %s" % data["error"])
    if data.get("stdout"):
        lines.extend(["stdout:", data["stdout"].rstrip()])
    if data.get("stderr"):
        lines.extend(["stderr:", data["stderr"].rstrip()])
    if data.get("stdout_truncated") or data.get("stderr_truncated"):
        lines.append("  output truncated: true")
    return "\n".join(lines)
