"""Identity-preserving compatibility redirect for the packaged queue ledger.

The immutable queued-actions baseline still imports this historical root name
to obtain its schema.  Redirecting the module object itself keeps that import
working while ensuring callers observe the canonical packaged implementation,
rather than a second delegating module namespace.
"""
from __future__ import annotations

import sys

from sonder_runtime.adapters.persistence import queued_actions as _implementation

sys.modules[__name__] = _implementation
