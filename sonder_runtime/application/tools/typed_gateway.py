"""Typed-port composition for the existing tool gateway.

The gateway remains the authority for cross-cutting request controls.  This
module only replaces its schema and invocation collaborators with adapters
over the application-owned registry and executor ports.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

from ...domain.common.errors import InvalidInput
from ...application.context import OperationContext
from ...application.ports.tool_execution import (
    ToolExecutionResult,
    ToolExecutor,
    ToolPolicy,
)
from ...application.ports.tool_registry import (
    ToolCall,
    ToolDescriptor,
    ToolRegistry,
    validate_tool_call,
)


@dataclass(frozen=True)
class PortInvocationOutput:
    """Structural equivalent of the gateway's provider output DTO."""

    success: bool
    output: Any = ""
    error_code: str = ""
    error: str = ""
    metadata: Mapping[str, Any] = ()


class _CancellationAdapter:
    def __init__(self, signal: Any) -> None:
        self._signal = signal

    @property
    def cancelled(self) -> bool:
        return bool(self._signal is not None and self._signal.cancelled)

    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        return self.cancelled


def default_tool_context(request: Any) -> OperationContext:
    """Create an explicit operation context without widening request scope."""
    return OperationContext(
        correlation_id=request.request_id,
        principal_id=request.scope.principal_id,
        auth_level="local",
        source="repl",
        deadline_monotonic=request.deadline_monotonic,
        cancellation=_CancellationAdapter(request.cancellation),
        workspace_roots=tuple(Path(root) for root in request.scope.workspace_roots),
    )


class RegistrySchemaValidator:
    """Validate requests against descriptors owned by a typed registry."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def validate(self, tool_name: str, arguments: Mapping[str, Any]) -> None:
        descriptor = self._registry.get(tool_name)
        if descriptor is None:
            raise InvalidInput(f"unknown tool {tool_name!r}")
        validate_tool_call(
            descriptor,
            ToolCall(tool_name=tool_name, arguments=dict(arguments)),
        )


class PortBackedToolInvoker:
    """Invoke a typed executor after typed policy selection."""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: ToolPolicy,
        executor: ToolExecutor,
        *,
        context_factory: Callable[[Any], OperationContext] = default_tool_context,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._executor = executor
        self._context_factory = context_factory

    def invoke(self, request: Any) -> PortInvocationOutput:
        descriptor = self._registry.get(request.tool_name)
        if descriptor is None:
            raise InvalidInput(f"unknown tool {request.tool_name!r}")
        call = ToolCall(
            tool_name=request.tool_name,
            arguments=dict(request.arguments),
            call_id=request.request_id,
        )
        context = self._context_factory(request)
        self._policy.authorize(descriptor, call, context)
        execution_class = self._policy.select_execution_class(descriptor)
        started = time.monotonic()
        result = self._executor.execute(descriptor, call, context, execution_class)
        elapsed = max(0, int((time.monotonic() - started) * 1000))
        if not isinstance(result, ToolExecutionResult):
            raise TypeError("typed tool executor returned an invalid result")
        return PortInvocationOutput(
            success=result.success,
            output=result.output,
            error_code=result.error_code,
            error=result.error,
            metadata={**dict(result.metadata), "duration_ms": result.duration_ms or elapsed},
        )


__all__ = [
    "PortBackedToolInvoker",
    "PortInvocationOutput",
    "RegistrySchemaValidator",
    "default_tool_context",
]
