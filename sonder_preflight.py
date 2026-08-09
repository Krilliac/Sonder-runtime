"""Compatibility alias for the packaged startup-preflight adapter.

New application callers use the typed preflight service.  Aliasing the module
object preserves private-helper monkeypatching and type identity for historical
``import sonder_preflight`` callers while the root import surface phases out.
"""
from __future__ import annotations

import sys

from sonder_runtime.adapters import preflight as _implementation

sys.modules[__name__] = _implementation
