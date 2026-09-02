"""Application-owned MCP composition for the native transport.

This is intentionally a bounded migration surface.  Its catalog is derived
from the tools currently owned by ``ToolExecutorAdapter``; the historical
server catalog remains a separate, explicit compatibility mode until parity
is demonstrated.
"""
from __future__ import annotations

import base64
import json
import sys
import uuid
from pathlib import Path
from typing import TextIO

from ..application.context import local_owner_context
from ..application.ports.tool_executor import ToolCall
from ..application.ports.tool_registry import (
    InMemoryToolRegistry,
    ToolCall as RegistryToolCall,
    ToolDescriptor,
    validate_tool_call,
)
from ..application.protocol.mcp_compatibility import (
    McpCompatibility,
    SUPPORTED_MCP_PROTOCOL_VERSIONS,
)
from ..application.protocol.mcp_tasks import McpTaskHandler
from ..interfaces.mcp.transport import McpTransportError, StdioMcpTransport


_PATH = {"type": "string", "minLength": 1}
_ROOT = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_ARCHIVE_ENTRIES = {"type": "integer", "minimum": 1, "maximum": 10_000}
_ARCHIVE_FILE_BYTES = {"type": "integer", "minimum": 1, "maximum": 256_000_000}
_ARCHIVE_TOTAL_BYTES = {"type": "integer", "minimum": 1, "maximum": 1_000_000_000}
_ARCHIVE_RATIO = {"type": "number", "minimum": 1, "maximum": 1_000.0}
_ARCHIVE_PATH_DEPTH = {"type": "integer", "minimum": 1, "maximum": 128}
_ARCHIVE_RESULTS = {"type": "integer", "minimum": 1, "maximum": 10_000}
_ARCHIVE_SECONDS = {"type": "number", "minimum": 1, "maximum": 60.0}
_ARCHIVE_CREATE_FILES = {"type": "integer", "minimum": 1, "maximum": 10_000}
_ARCHIVE_CREATE_ENTRIES = {"type": "integer", "minimum": 1, "maximum": 20_000}
_ARCHIVE_CREATE_DEPTH = {"type": "integer", "minimum": 1, "maximum": 64}
_ARCHIVE_INPUTS_JSON = {"type": "string", "minLength": 2, "maxLength": 1_000_000}
_COMPUTE_WORKLOADS = [
    "analysis", "build", "container", "embedding", "encode", "fuzz",
    "index", "render", "service", "storage", "test", "training",
]
_COMPUTE_TOOLS = (
    ToolDescriptor(
        "compute_submit",
        "Place one bounded catalog job locally or on an explicitly authorized private node",
        {"type": "object", "properties": {
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 128},
            "workload": {"type": "string", "enum": _COMPUTE_WORKLOADS},
            "catalog_entry_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "workspace_mapping": {"type": "string", "minLength": 1, "maxLength": 128},
            "relative_cwd": {"type": "string", "minLength": 1, "maxLength": 4096},
            "arguments": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
            "environment": {
                "type": "object", "additionalProperties": {"type": "string"},
                "maxProperties": 32,
            },
            "input_artifacts": {
                "type": "array", "maxItems": 64,
                "items": {"type": "object", "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "size_bytes": {"type": "integer", "minimum": 0, "maximum": 1 << 40},
                    "sha256": {"type": "string", "minLength": 64, "maxLength": 64},
                }, "required": ["name", "size_bytes", "sha256"],
                 "additionalProperties": False},
            },
            "deadline_seconds": {"type": "integer", "minimum": 1, "maximum": 86_400},
            "idempotent": _BOOL,
            "allow_remote": _BOOL,
            "allow_local_fallback": _BOOL,
        }, "required": [
            "request_id", "workload", "catalog_entry_id", "workspace_mapping",
            "allow_remote",
        ], "additionalProperties": False},
    ),
    ToolDescriptor(
        "compute_status", "Read one previously placed compute job",
        {"type": "object", "properties": {
            "controller_job_id": {"type": "string", "minLength": 1, "maxLength": 128},
        }, "required": ["controller_job_id"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "compute_cancel", "Cancel one previously placed compute job and prove cleanup",
        {"type": "object", "properties": {
            "controller_job_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "reason": {"type": "string", "minLength": 1, "maxLength": 512},
        }, "required": ["controller_job_id", "reason"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "compute_artifact_fetch",
        "Fetch one small digest-verified compute artifact through the authenticated transport",
        {"type": "object", "properties": {
            "controller_job_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "name": {"type": "string", "minLength": 1, "maxLength": 4096},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 98_304},
        }, "required": ["controller_job_id", "name"], "additionalProperties": False},
    ),
)
_NATIVE_TOOLS = (
    ToolDescriptor(
        "directory_tree", "List a bounded guarded directory tree",
        {"type": "object", "properties": {
            "path": {"type": "string"}, "depth": _INT, "max_entries": _INT,
            "include_hidden": _BOOL, "include_ignored": _BOOL, "extra_roots": _ROOT,
        }, "additionalProperties": False},
    ),
    ToolDescriptor(
        "directory_create", "Create a guarded directory and optional parents",
        {"type": "object", "properties": {
            "path": _PATH, "parents": {"type": "boolean"}, "extra_roots": _ROOT,
        }, "required": ["path"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "edit_file", "Apply a bounded text edit",
        {"type": "object", "properties": {
            "path": _PATH, "old": {"type": "string"}, "new": {"type": "string"},
            "count": {"type": "integer"}, "extra_roots": _ROOT,
        }, "required": ["path", "old", "new"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "file_edit", "Legacy alias for a bounded text edit",
        {"type": "object", "properties": {
            "path": _PATH, "old": {"type": "string"}, "new": {"type": "string"},
            "count": {"type": "integer"}, "extra_roots": _ROOT,
        }, "required": ["path", "old", "new"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "file_batch_write", "Transactionally create or overwrite bounded files",
        {"type": "object", "properties": {
            "operations": {"type": "array"}, "operations_json": {"type": "string"},
            "extra_roots": _ROOT,
        }, "additionalProperties": False},
    ),
    ToolDescriptor(
        "file_copy", "Copy one bounded binary-safe file",
        {"type": "object", "properties": {
            "source": _PATH, "destination": _PATH, "overwrite": _BOOL, "extra_roots": _ROOT,
        }, "required": ["source", "destination"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "file_delete", "Delete a guarded path only after explicit confirmation",
        {"type": "object", "properties": {
            "path": _PATH, "recursive": _BOOL, "dry_run": _BOOL,
            "confirm": {"type": "string"}, "extra_roots": _ROOT,
        }, "required": ["path"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "file_find", "Find files under allowed roots",
        {"type": "object", "properties": {
            "query": {"type": "string"}, "root": {"type": "string"},
            "max_results": _INT, "extra_roots": _ROOT,
            "include_ignored": _BOOL,
        }, "additionalProperties": False},
    ),
    ToolDescriptor(
        "file_move", "Move one bounded binary-safe file",
        {"type": "object", "properties": {
            "source": _PATH, "destination": _PATH, "overwrite": _BOOL, "extra_roots": _ROOT,
        }, "required": ["source", "destination"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "file_read", "Read a UTF-8-ish text file inside allowed roots",
        {"type": "object", "properties": {
            "path": _PATH, "max_bytes": {"type": "integer"}, "extra_roots": _ROOT,
        }, "required": ["path"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "file_read_range", "Read a bounded line range from a text file",
        {"type": "object", "properties": {
            "path": _PATH, "start_line": _INT, "end_line": _INT, "extra_roots": _ROOT,
        }, "required": ["path"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "file_write", "Create, overwrite, or append a text file inside allowed roots",
        {"type": "object", "properties": {
            "path": _PATH, "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["create", "overwrite", "append"]},
            "extra_roots": _ROOT,
        }, "required": ["path", "content"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "json_patch", "Preview or atomically apply a bounded JSON patch",
        {"type": "object", "properties": {
            "path": _PATH, "operations": {"type": "array"},
            "operations_json": {"type": "string"},
            "mode": {"type": "string", "enum": ["preview", "apply"]},
            "extra_roots": _ROOT,
        }, "required": ["path"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "image_inspect", "Inspect bounded image metadata and hash",
        {"type": "object", "properties": {"path": _PATH, "extra_roots": _ROOT},
         "required": ["path"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "make_directory", "Create a directory under an allowed root", {"type": "object"},
    ),
    ToolDescriptor(
        "read_file", "Read a bounded file", {"type": "object"},
    ),
    ToolDescriptor(
        "run_program", "Run an argv-based program", {"type": "object"},
    ),
    ToolDescriptor(
        "run_script", "Run a bounded script", {"type": "object"},
    ),
    ToolDescriptor(
        "program_search", "Search the executable path for programs",
        {"type": "object", "properties": {
            "query": {"type": "string"}, "max_results": _INT,
        }, "additionalProperties": False},
    ),
    ToolDescriptor(
        "script_search", "Find scripts under allowed roots",
        {"type": "object", "properties": {
            "query": {"type": "string"}, "root": {"type": "string"},
            "max_results": _INT, "max_entries": _INT, "timeout_seconds": {"type": "number"},
            "include_hidden": _BOOL, "include_ignored": _BOOL, "extra_roots": _ROOT,
        }, "additionalProperties": False},
    ),
    ToolDescriptor(
        "secret_scan", "Scan an authorized tree for redacted credential findings",
        {"type": "object", "properties": {
            "root": {"type": "string"}, "timeout": {"type": "number"},
            "extra_roots": _ROOT,
        }, "additionalProperties": False},
    ),
    ToolDescriptor(
        "text_search", "Search bounded text files under allowed roots",
        {"type": "object", "properties": {
            "query": {"type": "string", "minLength": 1}, "root": {"type": "string"},
            "glob": {"type": "string"}, "regex": _BOOL, "case_sensitive": _BOOL,
            "max_results": _INT, "max_file_bytes": _INT, "max_entries": _INT,
            "timeout_seconds": {"type": "number"}, "include_hidden": _BOOL,
            "include_ignored": _BOOL, "extra_roots": _ROOT,
        }, "required": ["query"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "text_patch", "Preview or transactionally apply a bounded unified diff",
        {"type": "object", "properties": {
            "root": _PATH, "patch": {"type": "string", "minLength": 1},
            "apply": _BOOL, "extra_roots": _ROOT,
        }, "required": ["root", "patch"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "workspace_run", "Run a program as a bounded argv list",
        {"type": "object", "properties": {
            "program": _PATH, "args_json": {"type": "string"}, "cwd": {"type": "string"},
            "stdin": {"type": "string"}, "timeout": {"type": "integer"},
            "max_output": {"type": "integer"}, "extra_roots": _ROOT,
        }, "required": ["program"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "write_file", "Write a file under an allowed root", {"type": "object"},
    ),
    ToolDescriptor(
        "archive_extract", "Extract a bounded archive transactionally without replacing a destination",
        {"type": "object", "properties": {
            "source": _PATH, "destination": _PATH,
            "max_entries": _ARCHIVE_ENTRIES, "max_file_bytes": _ARCHIVE_FILE_BYTES,
            "max_total_bytes": _ARCHIVE_TOTAL_BYTES, "max_ratio": _ARCHIVE_RATIO,
            "max_path_depth": _ARCHIVE_PATH_DEPTH, "max_results": _ARCHIVE_RESULTS,
            "max_seconds": _ARCHIVE_SECONDS, "extra_roots": _ROOT,
        }, "required": ["source", "destination"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "archive_create", "Create a bounded deterministic ZIP or TAR without overwriting",
        {"type": "object", "properties": {
            "root": _PATH, "inputs_json": _ARCHIVE_INPUTS_JSON, "destination": _PATH,
            "archive_format": {"type": "string", "enum": ["zip", "tar"]},
            "deterministic": _BOOL, "max_files": _ARCHIVE_CREATE_FILES,
            "max_entries": _ARCHIVE_CREATE_ENTRIES,
            "max_file_bytes": _ARCHIVE_FILE_BYTES,
            "max_total_bytes": _ARCHIVE_TOTAL_BYTES, "max_depth": _ARCHIVE_CREATE_DEPTH,
            "max_results": _ARCHIVE_RESULTS, "extra_roots": _ROOT,
        }, "required": ["root", "inputs_json", "destination"],
         "additionalProperties": False},
    ),
)

_INSPECTION_TOOLS = (
    ToolDescriptor(
        "archive_list", "Inspect a bounded archive without extracting it",
        {"type": "object", "properties": {
            "path": _PATH,
            "max_entries": _ARCHIVE_ENTRIES, "max_file_bytes": _ARCHIVE_FILE_BYTES,
            "max_total_bytes": _ARCHIVE_TOTAL_BYTES, "max_ratio": _ARCHIVE_RATIO,
            "max_path_depth": _ARCHIVE_PATH_DEPTH, "max_results": _ARCHIVE_RESULTS,
            "max_seconds": _ARCHIVE_SECONDS,
        },
         "required": ["path"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "artifact_risk_inspect", "Statically inspect a guarded artifact for risk",
        {"type": "object", "properties": {
            "path": _PATH, "max_scan_bytes": _INT,
            "max_seconds": {"type": "number"}, "extra_roots": _ROOT,
        }, "required": ["path"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "fetch_artifact", "Fetch and atomically verify a binary artifact",
        {"type": "object", "properties": {
            "url": {"type": "string", "minLength": 1}, "dest": _PATH,
            "expect_type": {"type": "string"}, "expect_publisher": {"type": "string"},
            "sha256": {"type": "string"}, "max_mb": {"type": "number"},
            "timeout": {"type": "number"}, "resume": _BOOL, "overwrite": _BOOL,
            "extra_roots": _ROOT,
        }, "required": ["url", "dest"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "data_inspect", "Inspect a bounded data file",
        {"type": "object", "properties": {"path": _PATH},
         "required": ["path"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "data_query", "Run a bounded read-only data query",
        {"type": "object", "properties": {"path": _PATH, "sql": {"type": "string"}},
         "required": ["path"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "dependency_inventory", "Inventory bounded project dependencies",
        {"type": "object", "properties": {"path": {"type": "string"}},
         "additionalProperties": False},
    ),
    ToolDescriptor(
        "directory_digest", "Digest a bounded directory manifest",
        {"type": "object", "properties": {"path": {"type": "string"}},
         "additionalProperties": False},
    ),
    ToolDescriptor(
        "file_digest", "Digest a bounded file",
        {"type": "object", "properties": {"path": _PATH},
         "required": ["path"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "log_inspect", "Inspect a bounded log file",
        {"type": "object", "properties": {"path": _PATH},
         "required": ["path"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "project_detect", "Detect project manifests and commands",
        {"type": "object", "properties": {"path": {"type": "string"}},
         "additionalProperties": False},
    ),
    ToolDescriptor(
        "process_list", "List bounded process metadata after explicit opt-in",
        {"type": "object", "properties": {
            "max_processes": _INT, "max_seconds": {"type": "number"},
        }, "additionalProperties": False},
    ),
    ToolDescriptor(
        "process_memory_risk_inspect", "Inspect bounded process memory risk indicators",
        {"type": "object", "properties": {
            "pid": {"type": "integer"}, "max_bytes": _INT,
            "max_regions": _INT, "max_seconds": {"type": "number"},
        }, "required": ["pid"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "verify_artifact", "Verify a guarded artifact already on disk",
        {"type": "object", "properties": {
            "path": _PATH, "expect_type": {"type": "string"},
            "expect_publisher": {"type": "string"}, "sha256": {"type": "string"},
            "extra_roots": _ROOT,
        }, "required": ["path"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "web_fetch", "Fetch bounded public web text with explicit consent",
        {"type": "object", "properties": {
            "url": {"type": "string", "minLength": 1},
            "max_chars": {"type": "integer"}, "consent": _BOOL,
        }, "required": ["url", "consent"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "web_search", "Search public web with explicit consent",
        {"type": "object", "properties": {
            "query": {"type": "string", "minLength": 1},
            "limit": {"type": "integer"}, "consent": _BOOL,
        }, "required": ["query", "consent"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "weather_lookup", "Get bounded weather with explicit consent",
        {"type": "object", "properties": {
            "location": {"type": "string", "minLength": 2},
            "forecast_days": {"type": "integer"},
            "units": {"type": "string", "enum": ["auto", "metric", "imperial"]},
            "consent": _BOOL,
        }, "required": ["location", "consent"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "approximate_location_lookup", "Resolve approximate location after explicit consent",
        {"type": "object", "properties": {"consent": _BOOL},
         "required": ["consent"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "vision_analyze", "Analyze one guarded local raster image",
        {"type": "object", "properties": {
            "path": _PATH, "prompt": {"type": "string", "minLength": 1},
        }, "required": ["path", "prompt"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "workspace_compare", "Compare two bounded workspaces",
        {"type": "object", "properties": {
            "left": _PATH, "right": _PATH,
        }, "required": ["left", "right"], "additionalProperties": False},
    ),
)
_NATIVE_TOOLS += _INSPECTION_TOOLS + _COMPUTE_TOOLS
_INSPECTION_NAMES = frozenset(item.name for item in _INSPECTION_TOOLS)
_VISION_NAMES = frozenset({"vision_analyze"})
_COMPUTE_NAMES = frozenset(item.name for item in _COMPUTE_TOOLS)


_LEGACY_ALIASES = {
    "directory_tree": "directory_tree",
    "directory_create": "make_directory",
    "file_edit": "edit_file",
    "file_find": "file_find",
    "file_read": "read_file",
    "file_read_range": "file_read_range",
    "file_write": "write_file",
    "program_search": "program_search",
    "script_search": "script_search",
    "text_search": "text_search",
    "workspace_run": "run_program",
}


def native_tool_registry() -> InMemoryToolRegistry:
    """Return the immutable-at-composition catalog for native MCP tools."""
    return InMemoryToolRegistry(sorted(_NATIVE_TOOLS, key=lambda item: item.name))


def run_native_mcp(application, *, input_stream: TextIO | None = None,
                   output_stream: TextIO | None = None,
                   task_handler=None) -> int:
    """Serve native MCP over stdio using the application tool port."""
    config = application.config
    roots = tuple(
        Path(root)
        for root in (config.state.workspace_roots if config is not None else ())
    )
    registry = native_tool_registry()
    if task_handler is None:
        job_service_factory = getattr(application, "job_service", None)
        if callable(job_service_factory):
            task_handler = McpTaskHandler(job_service_factory())
    capabilities = ("tools", "notifications")
    if callable(task_handler):
        capabilities += ("tasks",)

    def compute_result(name: str, arguments: dict) -> dict:
        from ..application.compute_fabric.jobs import DigestBoundInput, RemoteJobEnvelope
        from ..domain.compute_fabric import WorkloadKind, WorkloadRequest

        service_factory = getattr(application, "compute_service", None)
        if not callable(service_factory):
            return {
                "output": "compute fabric is not configured", "isError": True,
                "error": "DependencyUnavailable", "evidence": {},
            }
        try:
            service = service_factory()
            if name == "compute_submit":
                kind = WorkloadKind(arguments["workload"])
                request_id = arguments["request_id"]
                idempotency_key = arguments.get("idempotency_key", request_id)
                workspace = arguments["workspace_mapping"]
                allow_remote = arguments["allow_remote"]
                allow_local_fallback = arguments.get("allow_local_fallback", False)
                idempotent = arguments.get("idempotent", False)
                request = WorkloadRequest(
                    request_id=request_id,
                    kind=kind,
                    workspace_mapping=workspace,
                    allow_remote=allow_remote,
                    allow_local_fallback=allow_local_fallback,
                    idempotent=idempotent,
                )
                environment = arguments.get("environment", {})
                if not isinstance(environment, dict):
                    raise ValueError("environment must be an object")
                envelope = RemoteJobEnvelope.create(
                    controller_job_id=request_id,
                    idempotency_key=idempotency_key,
                    workload=kind,
                    catalog_entry_id=arguments["catalog_entry_id"],
                    workspace_mapping=workspace,
                    relative_cwd=arguments.get("relative_cwd", "."),
                    arguments=tuple(arguments.get("arguments", ())),
                    environment=tuple(sorted(environment.items())),
                    deadline_seconds=arguments.get("deadline_seconds", 300),
                    idempotent=idempotent,
                    input_artifacts=tuple(
                        DigestBoundInput(**dict(item))
                        for item in arguments.get("input_artifacts", ())
                    ),
                )
                submission = service.submit(request, envelope)
            elif name == "compute_status":
                submission = service.status(arguments["controller_job_id"])
            elif name == "compute_artifact_fetch":
                payload = service.fetch_artifact(
                    arguments["controller_job_id"],
                    arguments["name"],
                    max_bytes=arguments.get("max_bytes", 98_304),
                )
                artifact = payload.receipt
                output = {
                    "name": artifact.name,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.mime_type,
                    "sha256": artifact.sha256,
                    "content_base64": base64.b64encode(payload.content).decode("ascii"),
                }
                return {
                    "output": json.dumps(output, sort_keys=True, separators=(",", ":")),
                    "isError": False,
                    "error": None,
                    "evidence": {"sha256": artifact.sha256},
                }
            else:
                submission = service.cancel(
                    arguments["controller_job_id"], reason=arguments["reason"],
                )
        except Exception as exc:
            return {
                "output": str(exc), "isError": True,
                "error": type(exc).__name__, "evidence": {},
            }
        receipt = submission.receipt
        payload = {
            "node_id": submission.node_id,
            "remote_job_id": receipt.remote_job_id,
            "controller_job_id": receipt.controller_job_id,
            "idempotency_key": receipt.idempotency_key,
            "state": receipt.state,
            "request_sha256": receipt.request_sha256,
            "artifacts": [
                {
                    "name": artifact.name,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.mime_type,
                    "sha256": artifact.sha256,
                }
                for artifact in receipt.artifacts
            ],
            "output_preview": receipt.output_preview,
            "output_watermark": receipt.output_watermark,
            "output_truncated": receipt.output_truncated,
        }
        return {
            "output": json.dumps(payload, sort_keys=True, separators=(",", ":")),
            "isError": False,
            "error": None,
            "evidence": {
                "node_id": submission.node_id,
                "request_sha256": receipt.request_sha256,
            },
        }

    def execute(name: str, arguments: dict) -> dict:
        descriptor = registry.get(name)
        if descriptor is None:
            return {
                "output": "unknown native MCP tool: %s" % name,
                "isError": True,
                "error": "unknown_tool",
                "evidence": {},
            }
        try:
            validate_tool_call(
                descriptor, RegistryToolCall(tool_name=name, arguments=dict(arguments))
            )
        except Exception as exc:
            raise McpTransportError(str(exc)) from exc
        canonical_name = _LEGACY_ALIASES.get(name, name)
        canonical_arguments = dict(arguments)
        context = local_owner_context(
            correlation_id=uuid.uuid4().hex,
            source="mcp",
            workspace_roots=roots,
            timeout_seconds=60.0,
        )
        if canonical_name in _COMPUTE_NAMES:
            if canonical_name in {"compute_submit", "compute_cancel"}:
                from ..adapters.security.permission_policy import permission_policy

                decision = permission_policy.decide_for_caller(
                    canonical_name,
                    interactive=False,
                    gate_control_exempt=False,
                    surface="native-mcp",
                )
                if (
                    decision is not None
                    and decision.action != permission_policy.allow_action()
                ):
                    return {
                        "output": "compute host control denied by runtime permission policy",
                        "isError": True,
                        "error": "permission_denied",
                        "evidence": {"tool": canonical_name},
                    }
            return compute_result(canonical_name, canonical_arguments)
        cloud_consent = bool(canonical_arguments.pop("consent", False)) if canonical_name in {"web_fetch", "web_search", "weather_lookup", "approximate_location_lookup"} else False
        if cloud_consent:
            context = local_owner_context(
                correlation_id=context.correlation_id,
                source="mcp",
                workspace_roots=roots,
                cloud_allowed=True,
                timeout_seconds=60.0,
            )
        if canonical_name in _VISION_NAMES:
            service = getattr(application, "vision", None)
            if service is None:
                return {
                    "output": "vision service is not configured",
                    "isError": True,
                    "error": "DependencyUnavailable",
                    "evidence": {},
                }
            try:
                vision = service.analyze(
                    canonical_arguments["path"], canonical_arguments["prompt"], context,
                )
            except Exception as exc:
                return {
                    "output": str(exc), "isError": True,
                    "error": type(exc).__name__, "evidence": {},
                }
            return {
                "output": vision.text,
                "isError": False,
                "error": None,
                "evidence": {"model": vision.model, "tier": vision.tier},
            }
        if canonical_name in _INSPECTION_NAMES:
            result = application.inspections.inspect(
                canonical_name, canonical_arguments, context
            )
        else:
            result = application.tool_executor.execute(
                ToolCall(tool=canonical_name, arguments=canonical_arguments), context
            )
        return {
            "output": result.output,
            "isError": not result.ok,
            "error": result.error_code,
            "evidence": dict(result.evidence or {}),
        }

    transport = StdioMcpTransport(
        input_stream or sys.stdin,
        output_stream or sys.stdout,
        compatibility=McpCompatibility(
            server_version="2.0",
            supported_versions=SUPPORTED_MCP_PROTOCOL_VERSIONS,
            capabilities=capabilities,
        ),
        tool_catalog=registry,
        tool_handler=execute,
        task_handler=task_handler,
    )
    return transport.serve()


__all__ = ["native_tool_registry", "run_native_mcp"]
