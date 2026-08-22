"""Compatibility alias for the packaged learning-health adapter."""
from __future__ import annotations

import sys

from sonder_runtime.adapters import learning_health as _implementation

# Preserve the legacy module identity, private inspection surfaces, and
# monkeypatch seams while production callers migrate to the packaged adapter.
sys.modules[__name__] = _implementation
