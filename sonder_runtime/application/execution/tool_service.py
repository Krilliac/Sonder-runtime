"""Tool execution service (SPEC-5 §15).

Every model-authored tool call flows through ToolService:
descriptor lookup → schema validation → policy authorization →
execution-class selection → execute → normalize result → record.

No model-facing subsystem may invoke subprocess directly.
"""
from __future__ import annotations

import logging
import time
from typing import Protocol

from ..context import OperationContext

logger = logging.getLogger(__name__)
from ..ports.tool_executor import ToolResult as ExecutorResult
from ...domain.tools.descriptors import (
    ExecutionClass,
    ToolCall,
    ToolDescriptor,
    ToolResult,
)
from ...domain.tools.policy import ToolPolicy
from ...domain.common.errors import (
    Cancelled,
    DeadlineExceeded,
    InvalidInput,
)


class ToolRegistry(Protocol):
    def get(self, name: str) -> ToolDescriptor | None: ...
    def list_all(self) -> list[ToolDescriptor]: ...


class ToolExecutionAdapter(Protocol):
    def execute(
        self,
        descriptor: ToolDescriptor,
        call: ToolCall,
        context: OperationContext,
        execution_class: ExecutionClass,
    ) -> ToolResult: ...


class ToolService:
    """One gateway for all model-to-host tool calls."""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: ToolPolicy,
        executor: ToolExecutionAdapter,
    ):
        self._registry = registry
        self._policy = policy
        self._executor = executor

    def execute(
        self,
        call: ToolCall,
        context: OperationContext,
    ) -> ToolResult:
        logger.debug(f"ToolService.execute: tool_name={call.tool_name!r}")
        if context.expired:
            raise DeadlineExceeded("operation deadline exceeded before tool call")
        if context.cancellation is not None and context.cancellation.cancelled:
            raise Cancelled("operation cancelled before tool call")

        descriptor = self._registry.get(call.tool_name)
        if descriptor is None:
            raise InvalidInput(f"unknown tool {call.tool_name!r}")

        self._policy.authorize(descriptor, call)

        execution_class = self._policy.select_execution_class(descriptor)
        logger.debug(f"ToolService.execute: tool={call.tool_name!r}, execution_class={execution_class!r}")

        started = time.monotonic()
        result = self._executor.execute(descriptor, call, context, execution_class)
        elapsed = int((time.monotonic() - started) * 1000)

        if not result.success:
            logger.error(f"tool execution failed: tool={call.tool_name!r}, elapsed_ms={elapsed}")
            logger.warning(f"tool execution failed: tool={call.tool_name!r}, elapsed_ms={elapsed}, error={result.error!r}")
        elif elapsed > 10_000:
            logger.warning(f"slow tool execution: tool={call.tool_name!r}, elapsed_ms={elapsed}")
        logger.debug(f"ToolService.execute: tool={call.tool_name!r}, success={result.success}, elapsed_ms={elapsed}")
        return ToolResult(
            tool_name=call.tool_name,
            output=result.output,
            error=result.error,
            success=result.success,
            duration_ms=elapsed,
        )

    def list_tools(self) -> list[ToolDescriptor]:
        return self._registry.list_all()
