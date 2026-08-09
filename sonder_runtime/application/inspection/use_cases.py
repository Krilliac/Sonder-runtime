"""Read-only inspection facade independent of MCP and legacy modules."""
from __future__ import annotations

from ..context import OperationContext
from ..ports.tool_executor import ToolCall, ToolExecutor, ToolResult


INSPECTION_TOOLS = frozenset({
    "archive_list",
    "data_inspect",
    "data_query",
    "dependency_inventory",
    "directory_digest",
    "file_digest",
    "log_inspect",
    "project_detect",
    "workspace_compare",
})


class InspectionService:
    """Dispatch only the explicitly read-only inspection surface."""

    def __init__(self, executor: ToolExecutor) -> None:
        self._executor = executor

    def inspect(
        self, tool: str, arguments: dict, context: OperationContext
    ) -> ToolResult:
        if tool not in INSPECTION_TOOLS:
            return ToolResult(
                ok=False,
                error_code="unknown_inspection",
                output="unsupported read-only inspection: %s" % tool,
            )
        return self._executor.execute(
            ToolCall(tool=tool, arguments=dict(arguments or {})), context
        )
