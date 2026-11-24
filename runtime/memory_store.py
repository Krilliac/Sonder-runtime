"""Compatibility alias for the migrated SQLite memory adapter.

New code must import :mod:`sonder_runtime.adapters.memory_store`.  The true
module alias preserves globals, private-helper monkeypatches, and reload
behavior for legacy root callers while those imports migrate incrementally.
"""
from __future__ import annotations

import sys

from sonder_runtime.adapters import memory_store as _implementation

sys.modules[__name__] = _implementation
