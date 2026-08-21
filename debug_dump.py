"""Compatibility alias for the platform debug-dump helper."""

import sys as _sys

from sonder_runtime.platform import debug_dump as _debug_dump

_sys.modules[__name__] = _debug_dump
