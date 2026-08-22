"""Compatibility identity redirect for the packaged archive-create adapter."""
from __future__ import annotations

import sys

from sonder_runtime.adapters import archive_create as _implementation

# Keep legacy imports and monkeypatches attached to the canonical module.
sys.modules[__name__] = _implementation
