"""Compatibility identity redirect for the optional packaged slash menu."""
from __future__ import annotations

import sys

from sonder_runtime.adapters import optional_slash_menu_impl as _implementation

sys.modules[__name__] = _implementation
