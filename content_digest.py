"""Compatibility alias for the canonical content-digest inspection adapter."""

from __future__ import annotations

import sys as _sys

from sonder_runtime.adapters.inspection import content_digest as _implementation

_sys.modules[__name__] = _implementation
