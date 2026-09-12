#!/usr/bin/env python3
"""Restore reloadable_mcp.py from a1c8ddd4 and apply the loop docstring sync."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "reloadable_mcp.py"
BASE_SHA = "a1c8ddd4d0a522bfd79313771b32afcf479cc524"


def main() -> int:
    text = subprocess.check_output(
        ["git", "show", f"{BASE_SHA}:reloadable_mcp.py"],
        cwd=ROOT,
        text=True,
    )
    if "_sync_loop_tool_docstring" in text:
        TARGET.write_text(text, encoding="utf-8")
        print("base already contains sync helper")
        return 0
    if "import re\n" not in text:
        text = text.replace("import os\n", "import os\nimport re\n", 1)
    helper = '''

def _sync_loop_tool_docstring(fn, action_types) -> None:
    """Keep ``loop.__doc__`` vocabulary aligned with ``_LOOP_ACTION_TYPES``.

    FastMCP/MCPServer applies ``inspect.cleandoc`` when registering tools, which
    strips the indent before ``Argument shapes``. The in-module rewrite in
    ``server.py`` historically looked for that indent and silently no-oped, so
    the docstring lagged aliases the unknown-action reply already listed. Apply
    the sync here, after cleandoc, whenever the ``loop`` tool is registered.
    """
    if not action_types:
        return
    doc = fn.__doc__ or ""
    marker = "All valid `type` values:"
    marker_at = doc.find(marker)
    if marker_at < 0:
        return
    tail_match = re.search(r"\\n\\n[ \\t]*Argument shapes", doc[marker_at:])
    if tail_match is None:
        return
    tail_at = marker_at + tail_match.start()
    head = doc[: marker_at + len(marker)]
    fn.__doc__ = head + " " + ", ".join(action_types) + "." + doc[tail_at:]


'''
    needle = "class ReloadableMCPServer(MCPServer):"
    if needle not in text:
        print("class not found", file=sys.stderr)
        return 1
    text = text.replace(needle, helper + needle, 1)
    tool_override = '''
    def tool(self, *args, **kwargs):
        """Register a tool; keep ``loop`` docstring vocabulary in lockstep."""
        inner = super().tool(*args, **kwargs)

        def decorator(fn):
            result = inner(fn)
            tool_name = kwargs.get("name") or getattr(fn, "__name__", "")
            if tool_name != "loop" and getattr(fn, "__name__", "") != "loop":
                return result
            module = sys.modules.get(getattr(fn, "__module__", "") or "")
            action_types = getattr(module, "_LOOP_ACTION_TYPES", None) if module else None
            target = result if getattr(result, "__doc__", None) is not None else fn
            _sync_loop_tool_docstring(target, action_types)
            if target is not fn:
                _sync_loop_tool_docstring(fn, action_types)
            return result

        return decorator

'''
    marker2 = "    def add_resource(self, resource) -> None:"
    if marker2 not in text:
        print("add_resource not found", file=sys.stderr)
        return 1
    text = text.replace(marker2, tool_override + marker2, 1)
    TARGET.write_text(text, encoding="utf-8")
    print("wrote", TARGET)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
