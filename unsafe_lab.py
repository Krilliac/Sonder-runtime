"""Compatibility alias for the packaged unsafe-lab security gate."""
from sonder_runtime.application.security import unsafe_lab as _unsafe_lab
import sys as _sys

_sys.modules[__name__] = _unsafe_lab
