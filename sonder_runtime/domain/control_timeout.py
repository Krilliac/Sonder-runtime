"""Pure timeout parsing policy for interactive code-run commands."""

from __future__ import annotations


DEFAULT_TIMEOUT = 8
MAX_TIMEOUT = 60


def parse_control_timeout(arg, command="/run"):
    """Return ``(timeout, error)`` for a REPL control-command argument."""
    arg = (arg or "").strip()
    if not arg:
        return DEFAULT_TIMEOUT, None
    try:
        value = int(arg)
    except ValueError:
        return None, "usage: %s [seconds]  (runs the previous fenced code block)" % command
    return max(1, min(value, MAX_TIMEOUT)), None
