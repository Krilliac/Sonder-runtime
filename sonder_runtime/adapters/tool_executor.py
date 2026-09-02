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
from .filesystem.typed import GuardedFileSystemAdapter


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
            if call.tool in {"process_list", "process_memory_risk_inspect"}:
                import sonder_runtime.adapters.process_risk as process_risk

                operation = (
                    "list_processes" if call.tool == "process_list"
                    else "inspect_process_memory"
                )
                res = getattr(process_risk, operation)(**args)
                return ToolResult(
                    ok=bool(res.get("ok")),
                    output=json.dumps(res, sort_keys=True),
                    evidence=res,
                )
            if call.tool == "artifact_risk_inspect":
                import sonder_runtime.adapters.artifact_risk as artifact_risk

                res = artifact_risk.inspect_artifact(**args)
                return ToolResult(
                    ok=True,
                    output=artifact_risk.format_result(res),
                    evidence=res,
                )
            if call.tool == "archive_create":
                from sonder_runtime.adapters.archive_create import (
                    ArchiveCreateAdapter,
                    format_result,
                )
                from sonder_runtime.application.ports.archive_create import (
                    ArchiveCreateRequest,
                )

                archive_request = ArchiveCreateRequest(
                    root=args.pop("root"),
                    inputs_json=args.pop("inputs_json"),
                    destination=args.pop("destination"),
                    developer_authorized=context.auth_level in ("developer", "admin"),
                    **args,
                )
                res = ArchiveCreateAdapter().create_archive(archive_request)
                return ToolResult(
                    ok=bool(res.get("ok")),
                    output=format_result(res),
                    evidence=res,
                )
            if call.tool == "archive_extract":
                import sonder_runtime.adapters.inspection.archive_tools as archive_tools

                archive_args = dict(args)
                archive_args["source_path"] = archive_args.pop("source")
                archive_args["destination_path"] = archive_args.pop("destination")
                res = archive_tools.extract_archive(
                    **archive_args,
                    developer_authorized=context.auth_level in ("developer", "admin"),
                )
                return ToolResult(
                    ok=bool(res.get("ok")),
                    output=archive_tools.format_result(res),
                    evidence=res,
                )
            if call.tool == "secret_scan":
                import sonder_runtime.adapters.secret_scan as secret_scan

                res = secret_scan.scan(**args)
                return ToolResult(
                    ok=bool(res.get("ok")),
                    output=secret_scan.format_result(res), evidence=res,
                )
            if call.tool == "web_fetch":
                import sonder_runtime.adapters.web_fetch as web_fetch

                res = web_fetch.fetch(context=context, **args)
                evidence = {
                    key: value for key, value in res.items()
                    if key != "text"
                }
                return ToolResult(
                    ok=bool(res.get("ok")),
                    output=web_fetch.format_result(res), evidence=evidence,
                )
            if call.tool == "web_search":
                import sonder_runtime.adapters.web_search as web_search

                res = web_search.search(context=context, **args)
                return ToolResult(
                    ok=bool(res.get("ok")),
                    output=web_search.format_result(res),
                    evidence={"query": res.get("query"), "count": len(res.get("results", []))},
                )
            if call.tool == "weather_lookup":
                import sonder_runtime.adapters.weather as weather

                res = weather.lookup(context=context, **args)
                return ToolResult(
                    ok=bool(res.get("ok")), output=weather.format_result(res),
                    evidence={"location": res.get("location")},
                )
            if call.tool == "approximate_location_lookup":
                import sonder_runtime.adapters.location as location

                res = location.lookup(context=context, **args)
                return ToolResult(
                    ok=bool(res.get("ok")), output=location.format_result(res),
                    evidence={"label": res.get("label")},
                )
            if call.tool in {"fetch_artifact", "verify_artifact"}:
                import sonder_runtime.adapters.artifact_fetch as artifact_fetch

                if call.tool == "fetch_artifact":
                    res = artifact_fetch.fetch_artifact(**args)
                    output = artifact_fetch.format_fetch_result(res)
                else:
                    res = artifact_fetch.verify_artifact(**args)
                    output = artifact_fetch.format_verify_result(res)
                return ToolResult(
                    ok=bool(res.get("ok")), output=output, evidence=res,
                )
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
                import sonder_runtime.adapters.filesystem.file_ops as file_ops
                import sonder_runtime.adapters.filesystem.workbench as workbench

                # The same secret/control-plane read guard the in-module read
                # tools enforce, applied before the workbench primitive is
                # touched: a bounded line range of a secret is still a read
                # of a secret.
                developer_authorized = bool(args.pop("developer_authorized", False)) or (
                    context.auth_level in ("developer", "admin")
                )
                file_ops.require_read_access(
                    args["path"], extra_roots=args.get("extra_roots", ""),
                    bypass=bool(args.get("bypass", False)),
                    developer_authorized=developer_authorized,
                )
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
                from pathlib import Path

                from sonder_runtime.application.ports.filesystem import (
                    FileSystemOperation,
                    FileSystemRequest,
                    FileSystemResource,
                )

                request = FileSystemRequest(
                    operation=FileSystemOperation.READ,
                    resource=FileSystemResource(Path(args.pop("path"))),
                    max_bytes=args.pop("max_bytes", 256_000),
                    extra_roots=args.pop("extra_roots", ""),
                    bypass=args.pop("bypass", False),
                    developer_authorized=bool(args.pop("developer_authorized", False)) or (
                        context.auth_level in ("developer", "admin")
                    ),
                )
                if args:
                    raise TypeError("unsupported read_file arguments: %s" % sorted(args))
                typed = GuardedFileSystemAdapter().read(request, context)
                return ToolResult(
                    ok=True,
                    output=typed.content.decode("utf-8", errors="replace"),
                    evidence={
                        "path": str(typed.observation.resource.path),
                        "bytes": typed.observation.bytes_read,
                        "truncated": bool(typed.truncated),
                    },
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
