"""Compatibility alias for the canonical archive inspection adapter."""
from __future__ import annotations

import sys as _sys

from sonder_runtime.adapters.inspection import archive_tools as _implementation


# Alias the module object, rather than copying public names, so legacy callers
# retain private helpers and monkeypatch behavior while the implementation lives
# behind the adapter boundary.
_sys.modules[__name__] = _implementation
