"""Single compatibility boundary for the historical :mod:`server` module."""
from __future__ import annotations

from types import ModuleType

import server


def runtime() -> ModuleType:
    """Return the already-composed historical runtime module."""
    return server


__all__ = ["runtime"]
