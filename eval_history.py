"""Compatibility alias for the migrated evaluation-history adapter.

New code must import :mod:`sonder_runtime.adapters.evaluation_history_store`.
The true module alias preserves constants, private-helper monkeypatches, and
reload behavior for legacy callers while those imports migrate incrementally.
"""
from __future__ import annotations

import sys

from sonder_runtime.adapters import evaluation_history_store as _implementation

sys.modules[__name__] = _implementation
