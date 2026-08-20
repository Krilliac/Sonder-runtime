"""Temporary compatibility alias for the canonical workspace comparison adapter."""
from __future__ import annotations

import sys as _sys

from sonder_runtime.adapters.inspection import workspace_compare as _implementation

_sys.modules[__name__] = _implementation
