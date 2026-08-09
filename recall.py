"""Compatibility alias for the migrated semantic-recall adapter."""
from __future__ import annotations

import sys

from sonder_runtime.adapters import recall as _implementation

sys.modules[__name__] = _implementation
