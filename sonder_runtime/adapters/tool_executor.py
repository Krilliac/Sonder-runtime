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
from ..application.ports.web import WebToolsDisabled
from .filesystem.typed import GuardedFileSystemAdapter


_NETWORK_REFUSAL_TOOLS = frozenset({
    "web_fetch", "web_search", "weather_lookup", "approximate_location_lookup",
})


def _require_read(path, args, context: OperationContext) -> None:
    """Apply the secret/control-plane read guard ``file_read`` enforces.

    For the point-read tools whose primitives only resolve the path
    (``image_inspect``, ``artifact_risk_inspect``, ``verify_artifact``,
    ``archive_extract``): their hash, size, header or verdict is a read of
    the file, so a credential store is refused whatever the token and a
    protected path without developer authorization, exactly as the
    ``image_inspect`` server handler does before it calls the primitive.
    Only the knobs the call carries are honored; developer authorization is
    the scope's auth level.
    """
    import sonder_runtime.adapters.filesystem.file_ops as file_ops

    file_ops.require_read_access(
        str(path), extra_roots=args.get("extra_roots", ""),
        bypass=bool(args.get("bypass", False)),
        developer_authorized=context.auth_level in ("developer", "admin"),
    )


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
                if "operations" not in args:
                    raise ValueError("json_patch needs operations (or operations_json)")
                res = json_patch_tool.patch_json(**args)
                return ToolResult(ok=True, output=json.dumps(res, sort_keys=True), evidence=res)
            if call.tool == "text_patch":
                import sonder_runtime.adapters.filesystem.text_patch as text_patch

                res = text_patch.text_patch(**args)
                return ToolResult(ok=True, output=json.dumps(res, sort_keys=True), evidence=res)
            if call.tool == "image_inspect":
                import sonder_runtime.adapters.filesystem.workbench as workbench

                _require_read(args["path"], args, context)
                res = workbench.image_inspect(**args)
                return ToolResult(ok=True, output=json.dumps(res, sort_keys=True), evidence=res)
            if call.tool == "document_extract":
                import sonder_runtime.adapters.inspection.document_extract as document_extract

                _require_read(args["path"], args, context)
                res = document_extract.extract_document(
                    **args,
                    developer_authorized=context.auth_level in ("developer", "admin"),
                )
                return ToolResult(
                    ok=True,
                    output=document_extract.format_result(res),
                    evidence=res,
                )
            if call.tool == "media_inspect":
                import sonder_runtime.adapters.inspection.media_inspect as media_inspect

                _require_read(args["path"], args, context)
                res = media_inspect.inspect_media(
                    **args,
                    developer_authorized=context.auth_level in ("developer", "admin"),
                )
                return ToolResult(
                    ok=True,
                    output=media_inspect.format_result(res),
                    evidence=res,
                )
            if call.tool == "code_metrics":
                import sonder_runtime.adapters.inspection.code_metrics as code_metrics

                _require_read(args.get("path", "."), args, context)
                res = code_metrics.analyze_code_metrics(
                    **args,
                    developer_authorized=context.auth_level in ("developer", "admin"),
                )
                return ToolResult(
                    ok=True,
                    output=code_metrics.format_result(res),
                    evidence=res,
                )
            if call.tool == "schema_validate":
                import sonder_runtime.adapters.inspection.schema_validate as schema_validate

                _require_read(args["path"], args, context)
                if args.get("schema_path"):
                    _require_read(args["schema_path"], args, context)
                res = schema_validate.validate_schema(
                    **args,
                    developer_authorized=context.auth_level in ("developer", "admin"),
                )
                return ToolResult(
                    ok=True,
                    output=schema_validate.format_result(res),
                    evidence=res,
                )
            if call.tool == "language_matrix":
                import sonder_runtime.adapters.inspection.language_matrix as language_matrix

                res = language_matrix.inspect_language_matrix(**args)
                return ToolResult(
                    ok=True,
                    output=language_matrix.format_result(res),
                    evidence=res,
                )
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

                _require_read(args["path"], args, context)
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
                _require_read(archive_args["source_path"], archive_args, context)
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
                    _require_read(args["path"], args, context)
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

                if "operations_json" in args and "operations" not in args:
                    args["operations"] = args.pop("operations_json")
                if "operations" not in args:
                    raise ValueError("file_batch_write needs operations (or operations_json)")
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
                import sonder_runtime.adapters.artifact_risk as artifact_risk
                import sonder_runtime.adapters.filesystem.workbench as workbench

                # The same execution-risk gate as the script_run tool: the
                # configured policy applies to every script launch surface.
                path = args.pop("path")
                risk_policy = args.pop("risk_policy", "")
                trusted_roots = args.get("extra_roots", "") if args.get("bypass") else ""
                try:
                    risk, res = artifact_risk.run_script_under_policy(
                        path,
                        lambda sealed: workbench.run_script(
                            path, sealed_script=sealed, **args
                        ),
                        requested=risk_policy,
                        extra_roots=trusted_roots,
                    )
                except artifact_risk.ArtifactRiskDenied as exc:
                    return ToolResult(
                        ok=False,
                        error_code="ArtifactRiskDenied",
                        output="execution denied by effective policy %s: %s"
                        % (
                            exc.result.get("policy", "unknown"),
                            artifact_risk.format_result(exc.result),
                        ),
                        evidence={"artifact_risk": exc.result},
                    )
                res = dict(res)
                res["artifact_risk"] = risk
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
            if call.tool in {"write_file", "edit_file", "make_directory"}:
                import sonder_runtime.adapters.filesystem.file_ops as file_ops

                res = getattr(file_ops, call.tool)(**args)
                data = res if isinstance(res, dict) else {"result": res}
                return ToolResult(ok=True, output=json.dumps(data, sort_keys=True), evidence=data)
            return ToolResult(
                ok=False,
                error_code="unknown_tool",
                output="unsupported tool: %s" % call.tool,
            )
        except (PermissionError, ValueError, OSError, KeyError, TypeError) as exc:
            return ToolResult(
                ok=False, error_code=type(exc).__name__, output=str(exc)
            )
        except WebToolsDisabled as exc:
            # The default-off egress gate is a refusal, not a fault: report it
            # like any other guard so no surface turns it into an internal error.
            return ToolResult(
                ok=False, error_code="WebToolsDisabled", output=str(exc)
            )
        except RuntimeError as exc:
            # The transactional primitives (batch write, JSON patch, text
            # patch) refuse with a structured report on their exception. The
            # report is the failure's output, so a surface can render it as
            # the primitive's own callers always have; anything else is a
            # real fault and propagates.
            report = getattr(exc, "report", None)
            if not isinstance(report, dict):
                if call.tool in _NETWORK_REFUSAL_TOOLS:
                    # The network adapters refuse with RuntimeError when web
                    # tools are disabled or every provider failed: an
                    # operator-visible outcome, not a runtime fault.
                    return ToolResult(
                        ok=False, error_code=type(exc).__name__, output=str(exc),
                    )
                raise
            return ToolResult(
                ok=False, error_code=type(exc).__name__,
                output=json.dumps(report, sort_keys=True, default=str), evidence=report,
            )
