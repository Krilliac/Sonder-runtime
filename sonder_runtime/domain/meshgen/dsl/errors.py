"""The one exception the recorder lets out."""
from __future__ import annotations


class ScriptError(Exception):
    """A build script the recorder cannot accept; ``str()`` is ``line N: message`` (repair-loop friendly)."""

    def __init__(self, line, message):
        self.line = int(line) if isinstance(line, int) and line > 0 else 0
        self.message = str(message)
        super().__init__("line %d: %s" % (self.line, self.message))


__all__ = ["ScriptError"]
