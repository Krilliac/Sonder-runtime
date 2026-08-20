"""Compatibility alias for the canonical context policy boundary."""

import sys as _sys

from sonder_runtime.platform import context_policy as _canonical

_sys.modules[__name__] = _canonical
