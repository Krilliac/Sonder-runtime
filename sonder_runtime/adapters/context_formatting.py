"""Compatibility alias for the packaged observability health formatter."""
from __future__ import annotations

from sonder_runtime.adapters.observability.health_formatting import (
    format_context_health,
)

__all__ = ["format_context_health"]
