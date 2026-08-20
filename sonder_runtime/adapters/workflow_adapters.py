"""Compatibility imports for the former generic workflow adapters."""
from __future__ import annotations

from .workflow_loop_runner import LoopRunnerAdapter


# Keep the repository boundary in this compatibility module until its own
# migration slice; the loop-runner implementation has a canonical owner now.
LegacyLoopRunner = LoopRunnerAdapter
