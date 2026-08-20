"""Compatibility surface for the packaged startup capability adapter."""
from __future__ import annotations

from ..adapters.runtime_capabilities import (
    RuntimeCapabilities,
    current,
    freeze,
    _reset_for_tests,
)

__all__ = ["RuntimeCapabilities", "freeze", "current"]
