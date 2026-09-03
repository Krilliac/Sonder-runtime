"""Format standardized loop action result dicts from raw text output."""

from __future__ import annotations


def loop_text_result(action_type, text):
    """Build a loop action result from raw text output."""
    text = text or ""
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    return {
        "ok": not text.startswith("ERROR:"),
        "type": action_type,
        "summary": first_line[:200],
        "output": text,
    }
