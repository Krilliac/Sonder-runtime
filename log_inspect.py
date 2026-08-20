"""Compatibility alias for the canonical log inspection adapter."""
from __future__ import annotations

import sys as _sys

from sonder_runtime.adapters.inspection import log_inspect as _implementation


# Alias the module object so legacy callers retain public/private names and
# monkeypatch behavior while the implementation lives behind the adapter.
_sys.modules[__name__] = _implementation
