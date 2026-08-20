"""Compatibility boundary for the migrated dependency inventory adapter."""
from __future__ import annotations

import sys
from importlib import import_module as _import_module


_implementation = _import_module("sonder_runtime.adapters.inspection.dependency_inventory")

# Preserve true module identity so callers that patch public constants or
# private helpers continue to affect the implementation's global namespace.
sys.modules[__name__] = _implementation
