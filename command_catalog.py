"""Compatibility identity redirect for the packaged command catalog adapter."""
from __future__ import annotations

import sys

from sonder_runtime.adapters import command_catalog as _implementation

# Keep legacy imports, public helpers, private inspection surfaces, and
# monkeypatches attached to the canonical packaged module.
sys.modules[__name__] = _implementation
