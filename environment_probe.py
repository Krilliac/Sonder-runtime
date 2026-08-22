"""Compatibility import for the canonical packaged environment probe."""
from __future__ import annotations

import sys

from sonder_runtime.platform import environment_probe as _implementation

# Keep legacy imports and monkeypatch surfaces pointed at the implementation
# module, so callers observe one cache and one set of module globals.
sys.modules[__name__] = _implementation
