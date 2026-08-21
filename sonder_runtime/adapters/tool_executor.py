"""Canonical application-tool adapter.

This adapter owns the translation from the ``ToolExecutor`` port to the
guarded filesystem and workbench primitives.  The primitives remain the
authority for containment, authorization, and execution policy; this class
only maps their results into the application port's stable result shape.
"""
from __future__ import annotations

import json

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
            if call.tool == "json_patch":
                import sonder_runtime.adapters.filesystem.json_patch as json_patch_tool

                if "operations_json" in args and "operations" not in args:
                    args["operations"] = args.pop("operations_json")
                res = json_patch_tool.patch_json(**args)
                return ToolResult(ok=True, output=json.dumps(res, sort_keys=True), evidence=res)
            if call.tool == "text_patch":
                import sonder_runtime.adapters.filesystem.text_patch as text_patch

                res = text_patch.text_patch(**args)
                return ToolResult(ok=True, output=json.dumps(res, sort_keys=True), evidence=res)
            if call.tool == "image_inspect":
                import sonder_runtime.adapters.filesystem.workbench as workbench

                res = workbench.image_inspect(**args)
                return ToolResult(ok=True, output=json.dumps(res, sort_keys=True), evidence=res)
            if call.tool in {"file_copy", "file_move"}:
                import sonder_runtime.adapters.filesystem.file_ops as file_ops

                operation = "copy_file" if call.tool == "file_copy" else "move_file"
                res = getattr(file_ops, operation)(**args)
                return ToolResult(ok=True, output=json.dumps(res, sort_keys=True), evidence=res)
            if call.tool == "file_batch_write":
                import sonder_runtime.adapters.filesystem.file_ops as file_ops

                res = file_ops.batch_write_files(**args)
                return ToolResult(ok=True, output=json.dumps(res, sort_keys=True), evidence=res)
            if call.tool == "file_delete":
                import sonder_runtime.adapters.filesystem.file_ops as file_ops

                res = file_ops.delete_path(**args)
                return ToolResult(ok=True, output=json.dumps(res, sort_keys=True), evidence=res)
            if call.tool == "file_find":
                import sonder_runtime.adapters.filesystem.file_ops as file_ops

                res = file_ops.find_files(**args)
                return ToolResult(ok=True, output=json.dumps(res, sort_keys=True), evidence=res)
            if call.tool == "file_read_range":
                import sonder_runtime.adapters.filesystem.workbench as workbench

                res = workbench.read_line_range(**args)
                return ToolResult(ok=True, output=json.dumps(res, sort_keys=True), evidence=res)
            if call.tool in {"directory_tree", "text_search", "script_search", "program_search"}:
                import sonder_runtime.adapters.filesystem.workbench as workbench

                res = getattr(workbench, call.tool)(**args)
                return ToolResult(ok=True, output=json.dumps(res, sort_keys=True), evidence=res)
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
