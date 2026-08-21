"""Compatibility identity redirect for the packaged REPL command router."""
from __future__ import annotations

import sys

from sonder_runtime.interfaces.repl import command_router as _implementation

sys.modules[__name__] = _implementation
