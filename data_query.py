"""Temporary compatibility alias for the canonical data-query adapter."""
from __future__ import annotations

import sys as _sys

from sonder_runtime.adapters.inspection import data_query as _implementation

_sys.modules[__name__] = _implementation
