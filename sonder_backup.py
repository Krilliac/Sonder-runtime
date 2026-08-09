"""Compatibility alias for the migrated backup adapter."""
from __future__ import annotations

import sys

from sonder_runtime.adapters import backup as _implementation

sys.modules[__name__] = _implementation
