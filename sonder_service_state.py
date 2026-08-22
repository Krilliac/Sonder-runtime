"""Compatibility alias for the canonical packaged service-state boundary."""

from sonder_runtime.platform import service_state as _service_state
import sys as _sys

_sys.modules[__name__] = _service_state
