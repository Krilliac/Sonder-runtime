"""Compatibility alias for the durable served-action receipt adapter."""

import sys as _sys

from sonder_runtime.adapters.persistence import served_action_receipts as _receipts

_sys.modules[__name__] = _receipts
