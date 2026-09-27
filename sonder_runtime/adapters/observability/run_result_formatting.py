"""Operator-facing rendering for bounded command and tool runs."""
from __future__ import annotations

import json

from ...domain.diagnostics.digest import digest_text, render_digest


DIGEST_MAX_CHARS = 2000


def format_run_result(title: str, data: dict, *, digest: bool = False) -> str:
    """Render a harness result while preserving its diagnostic sections.

    ``digest=True`` appends, after everything else, a bounded ``digest:``
    block (final line, run summary, failure lines, first parsed errors) over
    the captured stdout and stderr. Without it the rendering is unchanged
    byte for byte.
    """
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
    # A host guard refusal carries its own bounded recovery advice; an MCP
    # caller that only sees this rendering must see it too.
    if data.get("guard"):
        reason = data.get("guard_reason")
        lines.append(
            "  guard: %s%s" % (data["guard"], " (%s)" % reason if reason else "")
        )
    if data.get("holder"):
        lines.append("  holder: %s" % data["holder"])
    if data.get("recovery"):
        lines.append("  recovery: %s" % data["recovery"])
    if data.get("stdout"):
        lines.extend(["stdout:", data["stdout"].rstrip()])
    if data.get("stderr"):
        lines.extend(["stderr:", data["stderr"].rstrip()])
    if data.get("stdout_truncated") or data.get("stderr_truncated"):
        lines.append("  output truncated: true")
    if digest and (data.get("stdout") or data.get("stderr")):
        combined = "%s\n%s" % (data.get("stdout") or "", data.get("stderr") or "")
        block = render_digest(
            digest_text(combined, source_kind="text"), max_chars=DIGEST_MAX_CHARS,
        )
        lines.append("digest:")
        lines.extend("  " + line for line in block.splitlines())
    return "\n".join(lines)
