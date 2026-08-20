"""Application-side execution contract for WP3 SEAM-002.

The policy is deliberately a separate port: authorization and execution-class
selection happen before an adapter is invoked.  Implementations of this
module never spawn processes, access files, or grant permissions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..context import OperationContext
from ...domain.tools.descriptors import ExecutionClass
from .tool_registry import ToolCall, ToolDescriptor


@dataclass(frozen=True)
class ToolExecutionResult:
    tool_name: str
    success: bool
    output: str = ""
    error_code: str = ""
    error: str = ""
    duration_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.success


class ToolPolicy(Protocol):
    """Authorization boundary owned by the application/domain policy."""

    def authorize(
        self, descriptor: ToolDescriptor, call: ToolCall, context: OperationContext
    ) -> None: ...

    def select_execution_class(self, descriptor: ToolDescriptor) -> ExecutionClass: ...


class ToolExecutor(Protocol):
    """Adapter boundary after lookup, validation, and policy authorization."""

    def execute(
        self,
        descriptor: ToolDescriptor,
        call: ToolCall,
        context: OperationContext,
        execution_class: ExecutionClass,
    ) -> ToolExecutionResult: ...


__all__ = ["ToolExecutionResult", "ToolExecutor", "ToolPolicy"]
