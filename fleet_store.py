"""Identity-preserving compatibility redirect for the packaged fleet store.

Production code imports ``sonder_runtime.adapters.persistence.fleet_store``
directly. This root name remains for the immutable fleet baseline migration
and other historical callers.
"""
from __future__ import annotations

import sys

from sonder_runtime.adapters.persistence import fleet_store as _implementation

sys.modules[__name__] = _implementation
