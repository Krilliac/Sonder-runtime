"""Strangler adapters: root-module implementations behind SPEC-3 ports.

The strangler strategy (SPEC-3 section 11) wraps the existing flat
modules as adapters first, then moves implementations behind the ports
use case by use case. Imports are deliberately lazy — building the
application graph must not drag in the 500KB server module until a
service is actually exercised.
"""
from __future__ import annotations

from .memory_repository import MemoryRepositoryAdapter
from .legacy_model_gateway import LegacyModelGateway
from .tool_executor import ToolExecutorAdapter
from .unit_of_work import UnitOfWorkAdapter


# Compatibility names for callers that still import the pre-migration adapter.
LegacyUnitOfWork = UnitOfWorkAdapter
LegacyMemoryRepository = MemoryRepositoryAdapter
LegacyToolExecutor = ToolExecutorAdapter
