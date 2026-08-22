"""Compatibility identity redirect for the packaged fanout persistence adapter."""
from __future__ import annotations

import sys

from sonder_runtime.adapters.persistence import fanout_store as _implementation

# Keep legacy imports, private inspection surfaces, and monkeypatches attached
# to the canonical packaged module used by production callers.
sys.modules[__name__] = _implementation
