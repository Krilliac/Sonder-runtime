"""Compatibility boundary for the migrated project detector.

The implementation lives under the packaged inspection adapter.  This root
module intentionally re-exports private helpers as well as public functions
because existing callers and tests historically imported this module directly.
"""
from __future__ import annotations

from importlib import import_module as _import_module


_implementation = _import_module("sonder_runtime.adapters.inspection.project_detect")
for _name, _value in vars(_implementation).items():
    if _name not in {"__name__", "__package__", "__loader__", "__spec__", "__file__"}:
        globals()[_name] = _value

__all__ = tuple(
    name for name in vars(_implementation)
    if not name.startswith("_")
)

del _name, _value, _implementation
