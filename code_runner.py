"""Backward-compatible identity alias for the packaged code-runner adapter."""

import sys

from sonder_runtime.adapters.execution_tools import code_runner as _implementation

# Preserve ``import code_runner`` monkeypatch and private-helper behavior while
# ensuring the implementation has one canonical packaged owner.
sys.modules[__name__] = _implementation
