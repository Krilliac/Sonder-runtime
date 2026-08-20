"""Compatibility imports for the former generic workflow adapters."""
from __future__ import annotations

from .workflow_repository import WorkflowRepositoryAdapter
from .workflow_loop_runner import LoopRunnerAdapter


# Compatibility names for callers that still import the former generic
# workflow adapters.
LegacyWorkflowRepository = WorkflowRepositoryAdapter
LegacyLoopRunner = LoopRunnerAdapter
