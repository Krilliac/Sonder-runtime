"""Pure policy for selecting replies that need the chat code gate."""

from __future__ import annotations

import re
from collections.abc import Callable


CODE_GATE_SIGNS = re.compile(
    r"^\s*(?:def\s+\w+|class\s+\w+|import\s+\w+|from\s+[\w.]+\s+import\s)",
    re.MULTILINE,
)


def code_gate_target(
    reply: str,
    extract_runnable_code_block: Callable[[str], dict | None],
) -> str | None:
    """Return runnable Python code worth compiling and smoke-running, or ``None``.

    The extractor is injected because it is an infrastructure concern owned by
    the legacy grounding module. This function owns only the gate's pure
    selection rules: fenced Python, meaningful definitions/imports, and no
    interactive stdin dependency.
    """
    if "```" not in str(reply or ""):
        return None
    block = extract_runnable_code_block(reply)
    if not block or block.get("language") != "python":
        return None
    code = block.get("code") or ""
    if not CODE_GATE_SIGNS.search(code):
        return None
    if re.search(r"\binput\s*\(", code):
        return None
    return code
