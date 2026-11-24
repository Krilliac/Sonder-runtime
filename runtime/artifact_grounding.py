"""Compatibility identity redirect for the packaged artifact-grounding adapter."""
from __future__ import annotations

import sys

from sonder_runtime.adapters import artifact_grounding as _implementation

# Keep legacy imports, private validation helpers, and monkeypatch seams
# attached to the canonical packaged module.
sys.modules[__name__] = _implementation
