"""Compatibility identity for the canonical packaged system-profile module."""

import importlib
import sys


_implementation = importlib.import_module("sonder_runtime.platform.system_profile")
sys.modules[__name__] = _implementation
