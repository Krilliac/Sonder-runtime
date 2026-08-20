"""Canonical application-tool adapter.

This adapter owns the translation from the ``ToolExecutor`` port to the
guarded filesystem and workbench primitives.  The primitives remain the
authority for containment, authorization, and execution policy; this class
only maps their results into the application port's stable result shape.
"""
from __future__ import annotations

from ..application.context import OperationContext
from ..application.ports.tool_executor import ToolCall, ToolResult


class ToolExecutorAdapter:
    """Execute the supported application tools through packaged guards."""

    def execute(self, call: ToolCall, context: OperationContext) -> ToolResult:
        args = dict(call.arguments or {})
        if context.expired:
            return ToolResult(
                ok=False,
                error_code="DeadlineExceeded",
                output="operation deadline exceeded",
            )
        if context.cancellation is not None and context.cancellation.cancelled:
            return ToolResult(
                ok=False,
                error_code="Cancelled",
                output="operation cancelled",
            )
        try:
            if call.tool == "run_program":
                import sonder_runtime.adapters.filesystem.workbench as workbench

                res = workbench.run_program(**args)
                return ToolResult(
                    ok=bool(res.get("ok")),
                    output=str(res.get("stdout", "")),
                    evidence=res,
                )
            if call.tool == "run_script":
                import sonder_runtime.adapters.filesystem.workbench as workbench

                res = workbench.run_script(**args)
                return ToolResult(
                    ok=bool(res.get("ok")),
                    output=str(res.get("stdout", "")),
                    evidence=res,
                )
            if call.tool == "read_file":
                import sonder_runtime.adapters.filesystem.file_ops as file_ops

                res = file_ops.read_file(**args)
                return ToolResult(
                    ok=True, output=str(res.get("text", "")), evidence=res
                )
            if call.tool == "write_file":
                import sonder_runtime.adapters.filesystem.file_ops as file_ops

                res = file_ops.write_file(**args)
                return ToolResult(
                    ok=True,
                    evidence=res if isinstance(res, dict) else {"result": res},
                )
            if call.tool == "edit_file":
                import sonder_runtime.adapters.filesystem.file_ops as file_ops

                res = file_ops.edit_file(**args)
                return ToolResult(
                    ok=True,
                    evidence=res if isinstance(res, dict) else {"result": res},
                )
            if call.tool == "make_directory":
                import sonder_runtime.adapters.filesystem.file_ops as file_ops

                res = file_ops.make_directory(**args)
                return ToolResult(ok=True, evidence=res)
            return ToolResult(
                ok=False,
                error_code="unknown_tool",
                output="unsupported tool: %s" % call.tool,
            )
        except (PermissionError, ValueError, OSError, KeyError, TypeError) as exc:
            return ToolResult(
                ok=False, error_code=type(exc).__name__, output=str(exc)
            )
