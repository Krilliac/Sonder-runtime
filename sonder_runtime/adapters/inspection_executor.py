"""Typed read-only inspection executor adapter."""
from __future__ import annotations

import json
import importlib
import os

from ..application.context import OperationContext
from ..application.ports.tool_executor import ToolCall, ToolResult


def _legacy_module(name: str):
    """Resolve a watched root module lazily without creating package cycles."""
    return importlib.import_module(name)


def _authorized(context: OperationContext) -> bool:
    return context.auth_level in ("user", "developer", "admin")


def _developer_authorized(context: OperationContext) -> bool:
    return context.auth_level in ("developer", "admin")


def _authorized_roots(context: OperationContext) -> str:
    return os.pathsep.join(str(path) for path in context.workspace_roots)


def _format_file_result(title: str, data: dict) -> str:
    lines = [title]
    for key, value in data.items():
        if key != "text":
            lines.append("  %s: %s" % (key, value))
    if "text" in data:
        lines.extend(["", data["text"]])
    return "\n".join(lines)


def _result(
    *, output: str, ok: bool, summary: str, audit_args: dict,
    activity: dict | None = None, record_output: bool = True,
) -> ToolResult:
    return ToolResult(
        ok=ok,
        output=output,
        evidence={
            "summary": summary,
            "audit_args": audit_args,
            "activity": activity or {},
            "record_output": record_output,
        },
    )


def _failure(exc: Exception, audit_args: dict) -> ToolResult:
    return ToolResult(
        ok=False,
        output=str(exc),
        error_code=type(exc).__name__,
        evidence={"summary": str(exc), "audit_args": audit_args},
    )


class InspectionExecutorAdapter:
    """Strict allowlisted adapter for read-only inspection modules."""

    def execute(self, call: ToolCall, context: OperationContext) -> ToolResult:
        args = dict(call.arguments or {})
        if context.expired:
            return ToolResult(
                ok=False, error_code="DeadlineExceeded",
                output="operation deadline exceeded",
            )
        if context.cancellation is not None and context.cancellation.cancelled:
            return ToolResult(
                ok=False, error_code="Cancelled", output="operation cancelled"
            )
        handlers = {
            "archive_list": self._archive_list,
            "data_inspect": self._data_inspect,
            "data_query": self._data_query,
            "dependency_inventory": self._dependency_inventory,
            "directory_digest": self._directory_digest,
            "file_digest": self._file_digest,
            "log_inspect": self._log_inspect,
            "project_detect": self._project_detect,
            "workspace_compare": self._workspace_compare,
        }
        handler = handlers.get(call.tool)
        if handler is None:
            return ToolResult(
                ok=False, error_code="unknown_inspection",
                output="unsupported read-only inspection: %s" % call.tool,
            )
        try:
            return handler(args, context)
        except Exception as exc:
            return _failure(exc, args)

    def _log_inspect(self, args: dict, context: OperationContext) -> ToolResult:
        log_inspect = _legacy_module("log_inspect")

        audit_args = {key: args[key] for key in (
            "path", "tail_lines", "context_lines", "max_file_bytes",
            "max_scan_bytes", "max_lines", "max_line_bytes", "max_results",
            "max_output_bytes", "timeout",
        )}
        try:
            report = log_inspect.inspect_log(
                args["path"], tail_lines=args["tail_lines"],
                context_lines=args["context_lines"],
                max_file_bytes=args["max_file_bytes"],
                max_scan_bytes=args["max_scan_bytes"],
                max_lines=args["max_lines"], max_line_bytes=args["max_line_bytes"],
                max_results=args["max_results"],
                max_output_bytes=args["max_output_bytes"], timeout=args["timeout"],
                extra_roots=_authorized_roots(context) if _authorized(context) else "",
            )
            output = log_inspect.encode_result(report)
            summary = report["summary"]
            activity_summary = "%d line(s), %d error(s), %d warning(s)%s" % (
                summary["lines_inspected"], summary["error_lines"],
                summary["warning_lines"],
                ", truncated" if report["scan_truncated"] or report["details_truncated"] else "",
            )
            return _result(
                output=output, ok=True, summary=activity_summary,
                audit_args=audit_args,
                activity={"summary": activity_summary, "path": report["path"]},
            )
        except Exception as exc:
            return _failure(exc, audit_args)


    def _workspace_compare(
        self, args: dict, context: OperationContext
    ) -> ToolResult:
        workspace_compare = _legacy_module("workspace_compare")

        audit_args = {key: args[key] for key in (
            "left", "right", "max_entries", "max_file_bytes",
            "max_total_bytes", "max_details", "max_output_bytes", "timeout",
        )}
        try:
            report = workspace_compare.compare_workspaces(
                args["left"], args["right"], max_entries=args["max_entries"],
                max_file_bytes=args["max_file_bytes"],
                max_total_bytes=args["max_total_bytes"],
                max_details=args["max_details"],
                max_output_bytes=args["max_output_bytes"], timeout=args["timeout"],
                extra_roots=(
                    _authorized_roots(context) if _authorized(context)
                    else args.get("extra_roots", "")
                ),
                bypass=_authorized(context),
                developer_authorized=_developer_authorized(context),
            )
            output = workspace_compare.encode_result(report)
            counts = report["summary"]
            summary = "added=%d removed=%d changed=%d same=%d" % (
                counts["added"], counts["removed"], counts["changed"], counts["same"],
            )
            activity_summary = "%d difference(s), %d same" % (
                counts["added"] + counts["removed"] + counts["changed"],
                counts["same"],
            )
            return _result(
                output=output, ok=True, summary=summary, audit_args=audit_args,
                activity={
                    "summary": activity_summary,
                    "path": "%s | %s" % (
                        report["left"]["root"], report["right"]["root"]
                    ),
                },
            )
        except Exception as exc:
            return _failure(exc, audit_args)


    def _project_detect(self, args: dict, context: OperationContext) -> ToolResult:
        project_detect = _legacy_module("project_detect")

        audit_args = {key: args[key] for key in (
            "path", "max_depth", "max_files", "max_total_bytes",
            "max_file_bytes", "max_results",
        )}
        try:
            data = project_detect.detect_project(
                path=args["path"], max_depth=args["max_depth"],
                max_files=args["max_files"], max_total_bytes=args["max_total_bytes"],
                max_file_bytes=args["max_file_bytes"], max_results=args["max_results"],
                extra_roots=_authorized_roots(context) if _authorized(context) else "",
            )
            summary = "%d manifest(s), %d command candidate(s)%s" % (
                len(data["manifests"]), len(data["commands"]),
                ", truncated" if data["truncated"] else "",
            )
            return _result(
                output=project_detect.format_detection(data), ok=not data["errors"],
                summary=summary, audit_args=audit_args,
                activity={"summary": summary, "path": data["root"]},
            )
        except Exception as exc:
            return _failure(exc, audit_args)

    def _file_digest(self, args: dict, context: OperationContext) -> ToolResult:
        content_digest = _legacy_module("content_digest")

        audit_args = {"path": args["path"], "max_bytes": args["max_bytes"]}
        try:
            data = content_digest.digest_file(
                args["path"], max_bytes=args["max_bytes"],
                extra_roots=_authorized_roots(context) if _authorized(context) else "",
            )
            summary = "%d byte(s)%s" % (
                data["bytes"], ", incomplete" if data["sha256"] is None else "",
            )
            return _result(
                output=content_digest.format_digest(data),
                ok=data["sha256"] is not None, summary=summary,
                audit_args=audit_args,
                activity={"summary": summary, "path": data["path"]},
            )
        except Exception as exc:
            return _failure(exc, audit_args)

    def _directory_digest(
        self, args: dict, context: OperationContext
    ) -> ToolResult:
        content_digest = _legacy_module("content_digest")

        audit_args = {key: args[key] for key in (
            "path", "max_depth", "max_files", "max_total_bytes",
            "max_file_bytes", "max_results",
        )}
        try:
            data = content_digest.digest_directory(
                args["path"], max_depth=args["max_depth"],
                max_files=args["max_files"], max_total_bytes=args["max_total_bytes"],
                max_file_bytes=args["max_file_bytes"], max_results=args["max_results"],
                extra_roots=_authorized_roots(context) if _authorized(context) else "",
            )
            summary = "%d file(s), %d byte(s)%s" % (
                len(data["manifest"]), data["bytes"],
                " complete" if data["complete"] else " incomplete",
            )
            return _result(
                output=content_digest.format_digest(data), ok=data["complete"],
                summary=summary, audit_args=audit_args,
                activity={"summary": summary, "path": data["root"]},
            )
        except Exception as exc:
            return _failure(exc, audit_args)

    def _archive_list(self, args: dict, context: OperationContext) -> ToolResult:
        archive_tools = _legacy_module("archive_tools")

        audit_args = {
            "path": args["path"], "max_entries": args["max_entries"],
            "max_results": args["max_results"],
        }
        try:
            if args.get("extra_roots") and not _authorized(context):
                raise PermissionError(
                    "extra_roots requires developer authorization or approval"
                )
            data = archive_tools.list_archive(
                args["path"], max_entries=args["max_entries"],
                max_file_bytes=args["max_file_bytes"],
                max_total_bytes=args["max_total_bytes"], max_ratio=args["max_ratio"],
                max_path_depth=args["max_path_depth"], max_results=args["max_results"],
                max_seconds=args["max_seconds"],
                extra_roots=_authorized_roots(context),
            )
            output = archive_tools.format_result(data)
            summary = "%d entries" % data.get("entry_count", 0)
            return _result(
                output=output, ok=data.get("valid", False), summary=summary,
                audit_args=audit_args,
                activity={"summary": summary, "path": data.get("source", "")},
            )
        except Exception as exc:
            return _failure(exc, audit_args)

    def _data_inspect(self, args: dict, context: OperationContext) -> ToolResult:
        file_ops = _legacy_module(
            "sonder_runtime.adapters.filesystem.file_ops"
        )

        audit_args = {"path": args["path"]}
        try:
            data = file_ops.inspect_data(
                args["path"], max_bytes=args["max_bytes"],
                extra_roots=(
                    _authorized_roots(context) if _authorized(context)
                    else args.get("extra_roots", "")
                ),
                bypass=_authorized(context),
                developer_authorized=_developer_authorized(context),
            )
            direct_summary = "%s %s bytes" % (
                data.get("kind", "?"), data.get("bytes", 0)
            )
            activity_summary = "%s (%s bytes)" % (
                data.get("kind", "?"), data.get("bytes", 0)
            )
            return _result(
                output=_format_file_result("data inspect", data), ok=True,
                summary=direct_summary, audit_args=audit_args,
                activity={
                    "summary": activity_summary, "path": data.get("path", "")
                },
                record_output=False,
            )
        except Exception as exc:
            return _failure(exc, audit_args)

    def _data_query(self, args: dict, context: OperationContext) -> ToolResult:
        data_query = _legacy_module("data_query")

        audit_args = {key: args[key] for key in (
            "path", "sql", "projection_json", "filters_json", "max_rows",
            "max_columns", "max_output_bytes", "max_scan_bytes", "timeout",
        )}
        try:
            result = data_query.query_data(
                args["path"], sql=args["sql"], projection=args["projection_json"],
                filters=args["filters_json"], max_rows=args["max_rows"],
                max_columns=args["max_columns"],
                max_output_bytes=args["max_output_bytes"],
                max_scan_bytes=args["max_scan_bytes"], timeout=args["timeout"],
                extra_roots=(
                    _authorized_roots(context) if _authorized(context)
                    else args.get("extra_roots", "")
                ),
                bypass=_authorized(context),
                developer_authorized=_developer_authorized(context),
            )
            output = data_query.encode_result(result)
            summary = "%s %d row(s)" % (result["kind"], result["count"])
            return _result(
                output=output, ok=True, summary=summary, audit_args=audit_args,
                activity={
                    "summary": "%s (%d row(s))" % (
                        result["kind"], result["count"]
                    ),
                    "path": result["path"],
                },
            )
        except Exception as exc:
            return _failure(exc, audit_args)

    def _dependency_inventory(
        self, args: dict, context: OperationContext
    ) -> ToolResult:
        dependency_inventory = _legacy_module("dependency_inventory")

        audit_args = {key: args[key] for key in (
            "path", "max_depth", "max_files", "max_total_bytes", "max_results",
        )}
        try:
            data = dependency_inventory.dependency_inventory(
                args["path"], max_depth=args["max_depth"],
                max_files=args["max_files"], max_total_bytes=args["max_total_bytes"],
                max_results=args["max_results"],
                extra_roots=(
                    _authorized_roots(context) if _authorized(context)
                    else args.get("extra_roots", "")
                ),
                bypass=_authorized(context),
            )
            output = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
            summary = "%d dependencies from %d files" % (
                len(data["items"]), data["files_read"]
            )
            return _result(
                output=output, ok=True, summary=summary, audit_args=audit_args
            )
        except Exception as exc:
            return _failure(exc, audit_args)


# Compatibility name for callers that still import the pre-migration adapter.
LegacyInspectionExecutor = InspectionExecutorAdapter
