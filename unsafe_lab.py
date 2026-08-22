"""Compatibility alias for the stateful unsafe-lab security adapter."""
from sonder_runtime.adapters.security import unsafe_lab as _unsafe_lab
import sys as _sys

_sys.modules[__name__] = _unsafe_lab
