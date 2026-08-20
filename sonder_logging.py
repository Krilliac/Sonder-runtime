"""Compatibility identity for the canonical packaged logging module."""
from sonder_runtime.platform import logging as _canonical
import sys as _sys

# Keep legacy imports and monkeypatches attached to the canonical module.
_sys.modules[__name__] = _canonical
