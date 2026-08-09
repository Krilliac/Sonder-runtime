"""Compatibility alias for the packaged saved-workflow store."""
from __future__ import annotations

import sys

from sonder_runtime.adapters.filesystem import workflow_store as _implementation

sys.modules[__name__] = _implementation
