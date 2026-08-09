"""Compatibility alias for the migrated process-liveness platform service.

New code must import :mod:`sonder_runtime.adapters.process_liveness`.  The
module alias preserves private-helper monkeypatch behavior for legacy callers
while the remaining root imports are migrated incrementally.
"""
from __future__ import annotations

import sys

from sonder_runtime.adapters import process_liveness as _implementation

sys.modules[__name__] = _implementation
