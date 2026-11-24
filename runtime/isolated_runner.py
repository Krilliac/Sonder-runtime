"""Compatibility identity for the package-owned container runner.

Keep the same module object so existing monkeypatches of private runner seams
still affect the implementation used by the direct MCP tool and Codegen.
"""
from __future__ import annotations

import sys

from sonder_runtime.adapters.execution import isolated_runner as _implementation

sys.modules[__name__] = _implementation
