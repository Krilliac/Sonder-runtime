"""The typed ``ToolExecutor`` port over the packaged canonical adapter.

``ToolExecutorAdapter`` owns the translation to the guarded filesystem and
workbench primitives and keeps the containment, authorization and execution
policy those primitives enforce.  This class only carries a typed-gateway
call (descriptor, call, context, execution class) to it and lifts its
result into the typed port's shape, so the typed gateway and the legacy
executor port run the very same code for a tool.
"""
from __future__ import annotations

import time

from ..application.context import OperationContext
from ..application.ports.tool_execution import ToolExecutionResult
from ..application.ports.tool_executor import ToolCall as LegacyToolCall
from ..application.ports.tool_registry import ToolCall, ToolDescriptor
from ..domain.tools.descriptors import ExecutionClass
from .tool_executor import ToolExecutorAdapter


class PackagedToolExecutor:
    """Execute a typed call through ``ToolExecutorAdapter``."""

    def __init__(self, adapter: ToolExecutorAdapter | None = None) -> None:
        self._adapter = adapter or ToolExecutorAdapter()

    def execute(
        self,
        descriptor: ToolDescriptor,
        call: ToolCall,
        context: OperationContext,
        execution_class: ExecutionClass,
    ) -> ToolExecutionResult:
        del execution_class  # the packaged guards decide how a tool runs
        started = time.monotonic()
        result = self._adapter.execute(
            LegacyToolCall(tool=descriptor.name, arguments=dict(call.arguments)), context,
        )
        elapsed = max(0, int((time.monotonic() - started) * 1000))
        return ToolExecutionResult(
            tool_name=descriptor.name,
            success=bool(result.ok),
            output=result.output,
            error_code="" if result.ok else (result.error_code or "error"),
            error="" if result.ok else str(result.output or result.error_code),
            duration_ms=elapsed,
            metadata={"evidence": dict(result.evidence or {})},
        )


__all__ = ["PackagedToolExecutor"]
