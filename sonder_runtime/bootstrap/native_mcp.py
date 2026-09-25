"""Application-owned MCP composition for the native transport.

This is intentionally a bounded migration surface.  Its catalog is derived
from the tools currently owned by ``ToolExecutorAdapter``; the historical
server catalog remains a separate, explicit compatibility mode until parity
is demonstrated.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import TextIO

logger = logging.getLogger(__name__)

from ..application.context import local_owner_context
from ..application.ports.tool_executor import ToolCall
from ..application.ports.tool_registry import (
    InMemoryToolRegistry,
    ToolDescriptor,
    validate_tool_call,
)
from ..application.ports.tool_registry import (
    ToolCall as RegistryToolCall,
)
from ..application.protocol.mcp_compatibility import (
    SUPPORTED_MCP_PROTOCOL_VERSIONS,
    McpCompatibility,
)
from ..application.protocol.mcp_tasks import McpTaskHandler
from ..domain.tools.descriptors import ExecutionClass, ToolEffect
from ..interfaces.mcp.transport import McpTransportError, StdioMcpTransport
from ..platform.version import runtime_version

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
            "placement_policy": {"type": "string", "enum": ["local-only", "prefer-remote", "rank-all"]},
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
        effects=frozenset({ToolEffect.WRITE_FILES}),
    ),
    ToolDescriptor(
        "edit_file", "Apply a bounded text edit",
        {"type": "object", "properties": {
            "path": _PATH, "old": {"type": "string"}, "new": {"type": "string"},
            "count": {"type": "integer"}, "extra_roots": _ROOT,
        }, "required": ["path", "old", "new"], "additionalProperties": False},
        effects=frozenset({ToolEffect.WRITE_FILES}),
    ),
    ToolDescriptor(
        "file_edit", "Legacy alias for a bounded text edit",
        {"type": "object", "properties": {
            "path": _PATH, "old": {"type": "string"}, "new": {"type": "string"},
            "count": {"type": "integer"}, "extra_roots": _ROOT,
        }, "required": ["path", "old", "new"], "additionalProperties": False},
        effects=frozenset({ToolEffect.WRITE_FILES}),
    ),
    ToolDescriptor(
        "file_batch_write", "Transactionally create or overwrite bounded files",
        {"type": "object", "properties": {
            "operations": {"type": "array"}, "operations_json": {"type": "string"},
            "extra_roots": _ROOT,
        }, "additionalProperties": False},
        effects=frozenset({ToolEffect.WRITE_FILES}),
    ),
    ToolDescriptor(
        "file_copy", "Copy one bounded binary-safe file",
        {"type": "object", "properties": {
            "source": _PATH, "destination": _PATH, "overwrite": _BOOL, "extra_roots": _ROOT,
        }, "required": ["source", "destination"], "additionalProperties": False},
        effects=frozenset({ToolEffect.WRITE_FILES}),
    ),
    ToolDescriptor(
        "file_delete", "Delete a guarded path only after explicit confirmation",
        {"type": "object", "properties": {
            "path": _PATH, "recursive": _BOOL, "dry_run": _BOOL,
            "confirm": {"type": "string"}, "extra_roots": _ROOT,
        }, "required": ["path"], "additionalProperties": False},
        effects=frozenset({ToolEffect.DELETE_FILES}),
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
        effects=frozenset({ToolEffect.WRITE_FILES, ToolEffect.DELETE_FILES}),
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
        effects=frozenset({ToolEffect.WRITE_FILES}),
    ),
    ToolDescriptor(
        "json_patch", "Preview or atomically apply a bounded JSON patch",
        {"type": "object", "properties": {
            "path": _PATH, "operations": {"type": "array"},
            "operations_json": {"type": "string"},
            "mode": {"type": "string", "enum": ["preview", "apply"]},
            "extra_roots": _ROOT,
        }, "required": ["path"], "additionalProperties": False},
        effects=frozenset({ToolEffect.WRITE_FILES}),
    ),
    ToolDescriptor(
        "image_inspect", "Inspect bounded image metadata and hash",
        {"type": "object", "properties": {"path": _PATH, "extra_roots": _ROOT},
         "required": ["path"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "make_directory", "Create a directory under an allowed root",
        {"type": "object", "properties": {
            "path": _PATH, "parents": {"type": "boolean"}, "extra_roots": _ROOT,
        }, "required": ["path"], "additionalProperties": False},
        effects=frozenset({ToolEffect.WRITE_FILES}),
    ),
    ToolDescriptor(
        "read_file", "Read a bounded file",
        {"type": "object", "properties": {
            "path": _PATH, "max_bytes": {"type": "integer"}, "extra_roots": _ROOT,
        }, "required": ["path"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "run_program", "Run an argv-based program",
        {"type": "object", "properties": {
            "program": _PATH, "args_json": {"type": "string"}, "cwd": {"type": "string"},
            "stdin": {"type": "string"}, "timeout": {"type": "integer"},
            "max_output": {"type": "integer"}, "extra_roots": _ROOT,
        }, "required": ["program"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "run_script", "Run a bounded script",
        {"type": "object", "properties": {
            "path": _PATH, "args_json": {"type": "string"}, "cwd": {"type": "string"},
            "stdin": {"type": "string"}, "timeout": {"type": "integer"},
            "max_output": {"type": "integer"}, "extra_roots": _ROOT,
        }, "required": ["path"], "additionalProperties": False},
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
        effects=frozenset({ToolEffect.WRITE_FILES}),
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
        "write_file", "Write a file under an allowed root",
        {"type": "object", "properties": {
            "path": _PATH, "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["create", "overwrite", "append"]},
            "extra_roots": _ROOT,
        }, "required": ["path", "content"], "additionalProperties": False},
        effects=frozenset({ToolEffect.WRITE_FILES}),
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
_AGENT_LANE_TOOL = ToolDescriptor(
    "agent_lane", "Open a fresh parent with open_parent; retain its scoped capability to control independent agent conversations. Never place the capability in task text or files.",
    {"type": "object", "properties": {
        "action": {"type": "string", "enum": [
            "open_parent", "rotate_parent", "revoke_parent", "spawn", "list", "inspect",
            "send_message", "wait", "interrupt", "resume", "cancel", "reports", "ack",
            "retrieve_archive",
        ]},
        "payload": {"type": "object", "maxProperties": 32},
        "parent_session_id": {"type": "string", "maxLength": 128},
        "parent_token": {"type": "string", "maxLength": 256},
    }, "required": ["action", "payload"], "additionalProperties": False},
)
_TOOL_CATEGORIES = [
    "compiler", "build_system", "test_runner", "linter_formatter", "debugger_profiler",
    "package_manager", "runtime", "container_vm", "vcs", "db_client", "media_doc",
    "cloud_cli", "editor_ide", "shell",
]
_TEST_RUNNERS = [
    "auto", "pytest", "unittest", "ctest", "cargo", "go", "dotnet", "npm", "pnpm",
    "yarn", "gradle", "maven", "make",
]
# Developer tools (bootstrap/developer_tools.py). The model chooses a category
# or name, a runner and a grammar-checked selector; never argv, env or paths
# to executables.
_DEVELOPER_TOOLS = (
    ToolDescriptor(
        "tool_inventory",
        "List developer tools installed on this host (compilers, build systems, test "
        "runners, linters, debuggers, package managers, runtimes, containers, VCS, DB "
        "clients, media/doc tools, cloud CLIs, editors, shells) with versions; filter by "
        "category or name; refresh re-probes with fixed read-only version switches.",
        {"type": "object", "properties": {
            "category": {"type": "string", "enum": _TOOL_CATEGORIES},
            "name": {"type": "string", "maxLength": 64},
            "refresh": _BOOL,
        }, "additionalProperties": False},
        effects=frozenset({ToolEffect.EXECUTE}),
        execution_class=ExecutionClass.HOST,
    ),
    ToolDescriptor(
        "test_run",
        "Run a project's test suite with a host-owned command for the detected runner "
        "(or the one named), optionally narrowed by a selector (pytest node id or "
        "k:<expr>, ctest name, cargo filter, ./go/pkg or run:TestName, dotnet filter, "
        "js path, jvm pattern), as a background job; returns a structured report "
        "(totals, failures with file:line) or a job id to poll with test_run_result.",
        {"type": "object", "properties": {
            "project": {"type": "string", "maxLength": 1024},
            "runner": {"type": "string", "enum": _TEST_RUNNERS},
            "selector": {"type": "string", "maxLength": 200},
            "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 1800},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 120},
            "workers": {"type": "integer", "minimum": 1, "maximum": 8},
        }, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ_FILES, ToolEffect.WRITE_FILES, ToolEffect.EXECUTE}),
        execution_class=ExecutionClass.HOST,
    ),
    ToolDescriptor(
        "test_run_result",
        "Wait (bounded) for a test_run job you started and return its structured report, "
        "or its status while it is still running.",
        {"type": "object", "properties": {
            "job_id": {"type": "string", "maxLength": 80},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 60},
        }, "required": ["job_id"], "additionalProperties": False},
    ),
    ToolDescriptor(
        "output_digest",
        "Summarize a test-run job's output or a guarded log file: final summary line, "
        "counts, FAILED/ERROR lines, first unique errors (file:line), grouped "
        "diagnostics and the tail. Give exactly one of job_id or path.",
        {"type": "object", "properties": {
            "job_id": {"type": "string", "maxLength": 80},
            "path": {"type": "string", "maxLength": 1024},
            "tail_lines": {"type": "integer", "minimum": 1, "maximum": 200},
            "max_failure_lines": {"type": "integer", "minimum": 1, "maximum": 200},
        }, "additionalProperties": False},
    ),
)
# C++ build tools (bootstrap/build_tools.py; docs/architecture/CPP-BUILD-FIX.md).
# The model names members of the parsed build model (targets, configs,
# platforms, presets, files) and a closed action set; the host renders the
# argv from closed templates. None of these names is a legacy tool name: the
# legacy ``build_run(root, command)`` keeps its own name and meaning.
_BUILD_GENERATORS = [
    "Ninja", "Ninja Multi-Config", "Unix Makefiles", "NMake Makefiles",
    "Visual Studio 17 2022", "Visual Studio 16 2019",
]
_BUILD_DETAILS = ["summary", "targets", "compile_units", "toolchain", "presets"]
_BUILD_ACTIONS = ["configure", "build", "compile_one", "include_trace"]
_BUILD_PRESET = {"type": "string", "pattern": "^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"}
_BUILD_PROJECT = {"type": "string", "maxLength": 1024}
_BUILD_TARGET = {"type": "string", "maxLength": 128}
_BUILD_WAIT = {"type": "integer", "minimum": 0, "maximum": 120}
_BUILD_JOB_ID = {"type": "string", "pattern": "^build-job-[0-9a-f]{16,32}$"}
_BUILD_FIX_ID = {"type": "string", "pattern": "^build-fix-[0-9a-f]{16,32}$"}
_BUILD_TOOLS = (
    ToolDescriptor(
        "build_model",
        "Describe a C/C++ project's build without running anything: build system, "
        "generator, configs, platforms, targets (utility and build-time-tool flags), "
        "toolchains, compile units, PCH and presets, read from the CMake File API "
        "reply, compile_commands.json or .sln/.vcxproj. Labels only, no host paths.",
        {"type": "object", "properties": {
            "project": _BUILD_PROJECT,
            "build_dir": _BUILD_PROJECT,
            "preset": _BUILD_PRESET,
            "detail": {"type": "string", "enum": _BUILD_DETAILS},
            "target": _BUILD_TARGET,
            "max_items": {"type": "integer", "minimum": 1, "maximum": 500},
            "refresh": _BOOL,
        }, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ_FILES}),
        execution_class=ExecutionClass.PURE,
    ),
    ToolDescriptor(
        "build_job",
        "Configure, build, compile one file, or trace one file's includes, with a "
        "host-owned command rendered from closed templates for the project's build "
        "system; every value must name a member of build_model. Runs as a background "
        "job and returns typed, attributed diagnostics or a job id for build_job_result.",
        {"type": "object", "properties": {
            "project": _BUILD_PROJECT,
            "build_dir": _BUILD_PROJECT,
            "action": {"type": "string", "enum": _BUILD_ACTIONS},
            "target": _BUILD_TARGET,
            "config": {"type": "string", "maxLength": 64},
            "platform": {"type": "string", "maxLength": 64},
            "preset": _BUILD_PRESET,
            "build_preset": _BUILD_PRESET,
            "file": _BUILD_PROJECT,
            "generator": {"type": "string", "enum": _BUILD_GENERATORS},
            "profile": {"type": "string", "maxLength": 64},
            "jobs": {"type": "integer", "minimum": 1, "maximum": 256},
            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 7200},
            "wait_seconds": _BUILD_WAIT,
            "allow_network": _BOOL,
        }, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ_FILES, ToolEffect.WRITE_FILES, ToolEffect.EXECUTE}),
        execution_class=ExecutionClass.HOST,
    ),
    ToolDescriptor(
        "build_job_result",
        "Wait (bounded) for a build_job you started and return its report, or its "
        "status while it is still running; cancel=true stops your own job instead.",
        {"type": "object", "properties": {
            "job_id": _BUILD_JOB_ID,
            "wait_seconds": _BUILD_WAIT,
            "cancel": _BOOL,
        }, "required": ["job_id"], "additionalProperties": False},
        effects=frozenset({ToolEffect.READ_FILES}),
    ),
    ToolDescriptor(
        "build_fix",
        "Repair a failing C/C++ build target with a bounded loop: compile the focus "
        "file, propose a patch confined to editable project sources (never build "
        "scripts or build-time-tool sources), verify with compile_one then a target "
        "build, keep the best candidate and revert regressions. Returns a job id for "
        "build_fix_result.",
        {"type": "object", "properties": {
            "project": _BUILD_PROJECT,
            "build_dir": _BUILD_PROJECT,
            "target": _BUILD_TARGET,
            "config": {"type": "string", "maxLength": 64},
            "platform": {"type": "string", "maxLength": 64},
            "focus_file": _BUILD_PROJECT,
            "attempts": {"type": "integer", "minimum": 1, "maximum": 8},
            "apply": _BOOL,
            "revert_after": _BOOL,
            "editable_globs": {"type": "array", "maxItems": 16,
                               "items": {"type": "string", "minLength": 1, "maxLength": 128}},
            "timeout_seconds": {"type": "integer", "minimum": 60, "maximum": 14400},
            "verify_dependents": _BOOL,
            "wait_seconds": _BUILD_WAIT,
            "allow_network": _BOOL,
        }, "required": ["target"], "additionalProperties": False},
        effects=frozenset({ToolEffect.READ_FILES, ToolEffect.WRITE_FILES, ToolEffect.EXECUTE}),
        execution_class=ExecutionClass.HOST,
    ),
    ToolDescriptor(
        "build_fix_result",
        "Wait (bounded) for a build_fix job you started and return its report: status, "
        "stop reason, attempts, per-file digests and diffs, and the final build; "
        "cancel=true stops your own fix and its child build instead.",
        {"type": "object", "properties": {
            "job_id": _BUILD_FIX_ID,
            "wait_seconds": _BUILD_WAIT,
            "cancel": _BOOL,
        }, "required": ["job_id"], "additionalProperties": False},
        effects=frozenset({ToolEffect.READ_FILES}),
    ),
    ToolDescriptor(
        "build_fix_restore",
        "Write a build_fix job's stored original files back (all, or the named ones) "
        "when each file still has the content the fix left; refuses a file changed since.",
        {"type": "object", "properties": {
            "job_id": _BUILD_FIX_ID,
            "files": {"type": "array", "maxItems": 6,
                      "items": {"type": "string", "minLength": 1, "maxLength": 1024}},
        }, "required": ["job_id"], "additionalProperties": False},
        effects=frozenset({ToolEffect.READ_FILES, ToolEffect.WRITE_FILES}),
    ),
)
_NATIVE_TOOLS += _INSPECTION_TOOLS + _COMPUTE_TOOLS + (_AGENT_LANE_TOOL,) + _DEVELOPER_TOOLS
_NATIVE_TOOLS += _BUILD_TOOLS
# Only the inspections the inspection service can run go to it. The catalog
# groups the web, weather, location, process and artifact tools with the
# inspections for presentation, but they run through the packaged executor;
# routing them by group sent nine native tools to a service that answered
# "unsupported read-only inspection" for every one of them.
from ..adapters.inspection_executor import (
    SUPPORTED_INSPECTIONS as _INSPECTION_NAMES,  # noqa: E402
)

_VISION_NAMES = frozenset({"vision_analyze"})
_COMPUTE_NAMES = frozenset(item.name for item in _COMPUTE_TOOLS)


# The read-only workbench family and the mutating file family run through the
# typed tool gateway when the application graph composed one
# (bootstrap/typed_tools.py); every other tool still reaches the packaged
# executor directly.
_TYPED_TOOL_NAMES = frozenset({
    "directory_tree", "file_find", "file_read_range", "program_search",
    "read_file", "script_search", "text_search",
    "edit_file", "file_batch_write", "file_copy", "file_delete", "file_move",
    "json_patch", "make_directory", "text_patch", "write_file",
    "output_digest", "test_run", "test_run_result", "tool_inventory",
    "build_model", "build_job", "build_job_result",
    "build_fix", "build_fix_result", "build_fix_restore",
})

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

# Canonical name -> the legacy name the permission catalog grades, derived
# from the alias table so this module does not import ``typed_tools`` (which
# derives its descriptors from this one). ``typed_tools.POLICY_NAMES`` is the
# declared map; a drift test pins the two together for the typed family.
_GRADED_NAMES = {
    canonical: legacy for legacy, canonical in _LEGACY_ALIASES.items()
    if legacy != canonical
}
# Native tools with no legacy alias whose legacy counterpart the catalog
# grades under another name. ``run_script`` runs the same workbench backend as
# the legacy ``script_run`` (an execution tool); ungraded, it is unclassified
# and refused in every mode, even ``auto``.
_GRADED_NAMES["run_script"] = "script_run"


def native_tool_registry() -> InMemoryToolRegistry:
    """Return the immutable-at-composition catalog for native MCP tools."""
    logger.debug(f"building native tool registry, tool_count={len(_NATIVE_TOOLS)}")
    return InMemoryToolRegistry(sorted(_NATIVE_TOOLS, key=lambda item: item.name))


def run_native_mcp(application, *, input_stream: TextIO | None = None,
                   output_stream: TextIO | None = None,
                   task_handler=None, close_compute_on_exit: bool = False,
                   progressive_tools: bool = False) -> int:
    """Serve native MCP over stdio using the application tool port."""
    logger.info("native MCP server starting")
    logger.debug("run_native_mcp starting")
    config = application.config
    roots = tuple(
        Path(root)
        for root in (config.state.workspace_roots if config is not None else ())
    )
    logger.debug(f"workspace roots={len(roots)}")
    registry = native_tool_registry()
    from ..application.ports.tool_registry import ToolSchemaSelection
    from ..application.tools.discovery import ToolDiscovery
    from ..application.tools.gateway_contract import (
        COMPLETED, FAILED, POLICY_DENIED, ToolGatewayRequest, ToolPermission,
        ToolReceipt, ToolScope,
    )
    discovery = ToolDiscovery(registry) if progressive_tools else None
    native_audit = getattr(application, "tool_audit", None) if discovery is not None else None
    if discovery is not None and not callable(getattr(native_audit, "append", None)):
        raise ValueError("progressive native tools require the host-composed durable audit")
    visible = ToolSchemaSelection()
    selection_generation = 0

    def audit_native(name, arguments, result, context, selection, *, policy_path, started,
                     selection_at_start=None):
        """Record compatibility outcomes without claiming typed gateway admission.

        The same repository instance also receives typed gateway receipts. Only
        digests and scope/selection metadata enter this record; tool output and
        arguments stay in their original guarded transport and executor paths.
        """
        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                               separators=(",", ":")).encode()).hexdigest()

        error = str(result.get("error") or "")
        failed = bool(result.get("isError"))
        request = ToolGatewayRequest(
            request_id=context.correlation_id, tool_name=name, arguments=dict(arguments),
            scope=ToolScope(context.principal_id, tuple(str(root) for root in context.workspace_roots),
                            source=context.source, auth_level=context.auth_level),
            permission=ToolPermission(), execution_world="local",
            schema_selection=selection,
        )
        receipt = ToolReceipt(
            request_id=context.correlation_id, tool_name=name, success=not failed,
            output="", error_code=error if failed else "", requester_id=context.principal_id,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            argument_digest=digest(arguments), result_digest=digest(result),
            execution_world="local", policy_match=policy_path,
            terminal=(POLICY_DENIED if error in {
                          "permission_denied", "tool_not_visible", "tool_schema_refused",
                      }
                      else FAILED if failed else COMPLETED),
            evidence={"inventory_digest": discovery.digest,
                      "selection_at_start": (selection_at_start or selection).marker(),
                      "native_compatibility_path": policy_path == "native_mcp_compatibility"},
        )
        native_audit.append(request, receipt)
    discovery_tools = InMemoryToolRegistry((
        ToolDescriptor("tool_search", "Find tools by name or purpose; returns bounded summaries only", {
            "type": "object", "properties": {
                "query": {"type": "string", "maxLength": 512},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            }, "required": ["query"], "additionalProperties": False,
        }),
        ToolDescriptor("tool_schema", "Load up to eight tool schemas, replacing the current visible selection", {
            "type": "object", "properties": {
                "names": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
                "inventory_digest": {"type": "string"},
            }, "required": ["names", "inventory_digest"], "additionalProperties": False,
        }),
    ))
    if task_handler is None:
        job_service_factory = getattr(application, "job_service", None)
        if callable(job_service_factory):
            logger.debug("wiring MCP task handler from job service")
            task_handler = McpTaskHandler(job_service_factory())
        else:
            logger.warning("MCP task handler unavailable, job_service not callable on application graph")
    capabilities = ("tools", "notifications")
    if callable(task_handler):
        capabilities += ("tasks",)
    logger.debug(f"MCP capabilities={capabilities!r}")

    def compute_result(name: str, arguments: dict) -> dict:
        from ..application.compute_fabric.jobs import (
            DigestBoundInput,
            RemoteJobEnvelope,
        )
        from ..domain.compute_fabric import (
            PlacementPolicy,
            WorkloadKind,
            WorkloadRequest,
        )

        service_factory = getattr(application, "compute_service", None)
        if not callable(service_factory):
            logger.warning(f"compute tool {name!r} called but compute fabric is not configured")
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
                    placement_policy=(PlacementPolicy(arguments["placement_policy"])
                                      if "placement_policy" in arguments else None),
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
            logger.error(f"compute operation failed, tool={name!r}, error={type(exc).__name__}", exc_info=True)
            logger.warning(f"compute operation {name!r} failed: {type(exc).__name__}", exc_info=True)
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

    def typed_result(tools, canonical_name: str, arguments: dict, context,
                     selection: ToolSchemaSelection | None) -> dict:
        """Run one workbench file tool through the typed gateway.

        The gateway is the single seam: schema, resource policy, the
        runtime's permission modes (as an unattended native-MCP caller, with
        the call's arguments so a one-shot approval of exactly this call can
        answer), deadline and cancellation, the packaged guards, redaction,
        and one durable receipt whatever the outcome. The envelope keeps its
        shape; a permission refusal reports the decision, call id included.
        """
        from ..application.tools.gateway_contract import (
            ToolGatewayRequest,
            ToolPermission,
            ToolScope,
        )
        from ..domain.common.errors import Cancelled, DeadlineExceeded, Forbidden

        descriptor = registry.get(canonical_name)
        if descriptor is None:
            raise ValueError("typed tool is absent from the native registry")
        # Only the host registry declares effects. Native input supplies tool
        # arguments, never a scope grant or permission effect list.
        effects = frozenset(effect.name.lower() for effect in descriptor.effects)

        request = ToolGatewayRequest(
            request_id=context.correlation_id,
            tool_name=canonical_name,
            arguments=arguments,
            scope=ToolScope(
                principal_id=context.principal_id,
                workspace_roots=tuple(str(root) for root in roots),
                allowed_effects=effects,
                source="mcp",
                auth_level=context.auth_level,
            ),
            permission=ToolPermission(effects),
            deadline_monotonic=context.deadline_monotonic,
            cancellation=context.cancellation,
            execution_world="local",
            schema_selection=(ToolSchemaSelection(
                frozenset(_LEGACY_ALIASES.get(name, name) for name in selection.visible_names),
                selection_id=selection.selection_id,
            ) if selection is not None else None),
        )
        # The roots a one-shot approval covered are honoured for this call
        # alone, and only once the gateway's evaluator has spent it: the
        # provider is consulted at resolution time, after the decision.
        from ..adapters.filesystem import file_ops
        from ..adapters.security.permission_policy import permission_policy

        graded = _GRADED_NAMES.get(canonical_name, canonical_name)

        def granted() -> str:
            if not permission_policy.approval_spent_for(graded, arguments):
                return ""
            roots = arguments.get("extra_roots", "")
            return roots if isinstance(roots, str) else ""

        try:
            with file_ops.reach_scope(granted):
                receipt = tools.execute(request)
        except Forbidden as exc:
            return {
                "output": str(exc), "isError": True, "error": "permission_denied",
                "evidence": {"tool": canonical_name, **dict(getattr(exc, "decision", {}) or {})},
            }
        except (Cancelled, DeadlineExceeded) as exc:
            return {
                "output": str(exc), "isError": True,
                "error": type(exc).__name__, "evidence": {"tool": canonical_name},
            }
        finally:
            permission_policy.forget_spent_approval()
        evidence = dict(receipt.evidence)
        evidence.update({
            "request_id": receipt.request_id,
            "argument_digest": receipt.argument_digest,
            "result_digest": receipt.result_digest,
            "terminal": receipt.terminal,
        })
        return {
            "output": receipt.output,
            "isError": not receipt.success,
            "error": receipt.error_code or None,
            "evidence": evidence,
        }

    def execute_inner(name: str, arguments: dict, *, selected: ToolSchemaSelection | None = None,
                      operation_context=None) -> dict:
        nonlocal visible, selection_generation
        logger.debug(f"MCP execute tool={name!r}")
        if discovery is not None:
            if name in {"tool_search", "tool_schema"}:
                from ..domain.common.errors import InvalidInput
                descriptor = discovery_tools.get(name)
                started = time.monotonic()
                selection_at_start = selected if selected is not None else visible
                discovery_context = operation_context or local_owner_context(
                    correlation_id=uuid.uuid4().hex, source="mcp", workspace_roots=roots,
                    timeout_seconds=60.0,
                )
                try:
                    validate_tool_call(descriptor, RegistryToolCall(tool_name=name, arguments=dict(arguments)))
                    if name == "tool_search":
                        payload = discovery.search(**arguments)
                        published = selection_at_start
                    else:
                        next_visible, payload = discovery.load(
                            arguments["names"], inventory_digest=arguments["inventory_digest"],
                            selection_id=f"mcp-tools:{selection_generation + 1}",
                        )
                        # The selected-schema digest is the request-visible identity.
                        # Loaded schemas confer no permission to execute effects.
                        published = ToolSchemaSelection(next_visible.visible_names,
                                                        selection_id=payload["manifest"]["digest"])
                except (InvalidInput, TypeError, ValueError) as exc:
                    # Keep caller text out of the durable audit. An invalid
                    # schema request must not replace the previous selection.
                    refused = {"output": "", "isError": True,
                               "error": ("tool_schema_refused" if name == "tool_schema"
                                         else "tool_search_invalid"), "evidence": {}}
                    try:
                        audit_native(name, arguments, refused, discovery_context,
                                     selection_at_start, policy_path="native_mcp_discovery",
                                     started=started, selection_at_start=selection_at_start)
                    except Exception as audit_error:
                        raise McpTransportError(
                            "native tool audit unavailable; reconcile outcome before retry"
                        ) from audit_error
                    raise McpTransportError(str(exc)) from exc
                result = {"output": json.dumps(payload, sort_keys=True), "isError": False,
                          "error": None, "evidence": {"inventory_digest": discovery.digest}}
                try:
                    audit_native(name, arguments, result, discovery_context, published,
                                 policy_path="native_mcp_discovery", started=started,
                                 selection_at_start=selection_at_start)
                except Exception as audit_error:
                    raise McpTransportError(
                        "native tool audit unavailable; reconcile outcome before retry"
                    ) from audit_error
                if name == "tool_schema":
                    visible = published
                    selection_generation += 1
                return result
            if not (selected if selected is not None else visible).allows(name):
                return {"output": "load the tool schema before calling this tool", "isError": True,
                        "error": "tool_not_visible", "evidence": {
                            "selection": (selected if selected is not None else visible).marker()}}
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
        context = operation_context or local_owner_context(
            correlation_id=uuid.uuid4().hex,
            source="mcp",
            workspace_roots=roots,
            timeout_seconds=60.0,
        )
        if canonical_name == "agent_lane":
            from ..adapters.security.permission_policy import permission_policy
            from ..domain.common.errors import SonderError
            from ..interfaces.agent_lane_entrypoint import (
                execute_lane_command,
                lane_approval_arguments,
            )
            try:
                safe = lane_approval_arguments(application, context, canonical_arguments)
                decision = permission_policy.decide_for_caller(
                    canonical_name, interactive=False, gate_control_exempt=False,
                    surface="native-mcp", arguments=safe,
                )
                if decision is not None and decision.action != permission_policy.allow_action():
                    return {"output": decision.reason, "isError": True, "error": "permission_denied",
                            "evidence": {"call_id": getattr(decision, "call_id", "")}}
                result = execute_lane_command(application, context, canonical_arguments)
                return {"output": json.dumps(result, ensure_ascii=False), "isError": False,
                        "error": None, "evidence": {"tool": canonical_name}}
            except (SonderError, ValueError, TypeError, PermissionError) as error:
                return {"output": str(error), "isError": True,
                        "error": "FORBIDDEN" if isinstance(error, PermissionError)
                        else getattr(error, "code", "INVALID_INPUT"), "evidence": {}}
            finally:
                permission_policy.forget_spent_approval()
        if canonical_name in _COMPUTE_NAMES:
            logger.debug(f"routing to compute handler: {canonical_name!r}")
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
                    logger.error(f"compute tool permission denied, tool={canonical_name!r}, surface='native-mcp'")
                    logger.warning(f"compute tool {canonical_name!r} denied by runtime permission policy")
                    return {
                        "output": "compute host control denied by runtime permission policy",
                        "isError": True,
                        "error": "permission_denied",
                        "evidence": {"tool": canonical_name},
                    }
            return compute_result(canonical_name, canonical_arguments)
        typed_tools = getattr(application, "tools", None)
        typed_route = canonical_name in _TYPED_TOOL_NAMES and typed_tools is not None
        if not typed_route:
            # The typed gateway applies the runtime permission modes itself;
            # every other native tool is gated here, as the legacy MCP surface
            # gates each call, so `plan`/`manual` hold for host programs,
            # archive writes, and downloads too. Graded under the name the
            # permission catalog knows; the call's own arguments let a
            # one-shot console approval of exactly this call answer it.
            from ..adapters.security.permission_policy import permission_policy

            graded = _GRADED_NAMES.get(canonical_name, canonical_name)
            try:
                decision = permission_policy.decide_for_caller(
                    graded, interactive=False, gate_control_exempt=False,
                    surface="native-mcp", arguments=dict(arguments),
                )
            finally:
                permission_policy.forget_spent_approval()
            if decision is not None and decision.action != permission_policy.allow_action():
                logger.warning(f"native tool {canonical_name!r} denied by runtime permission policy")
                return {
                    "output": "permission gate refused %s: %s" % (
                        canonical_name, getattr(decision, "reason", "") or "denied"),
                    "isError": True,
                    "error": "permission_denied",
                    "evidence": {"tool": canonical_name,
                                 "call_id": getattr(decision, "call_id", "")},
                }
        cloud_consent = bool(canonical_arguments.pop("consent", False)) if canonical_name in {"web_fetch", "web_search", "weather_lookup", "approximate_location_lookup"} else False
        if canonical_name == "approximate_location_lookup":
            # The location adapter demands the explicit consent flag as well as
            # cloud permission; the other web adapters take no such keyword.
            canonical_arguments["consent"] = cloud_consent
        if cloud_consent:
            context = local_owner_context(
                correlation_id=context.correlation_id,
                source="mcp",
                workspace_roots=roots,
                cloud_allowed=True,
                timeout_seconds=60.0,
            )
        if canonical_name in _VISION_NAMES:
            logger.debug(f"routing to vision service: {canonical_name!r}")
            service = getattr(application, "vision", None)
            if service is None:
                logger.warning(f"vision tool {canonical_name!r} called but vision service is not configured")
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
                logger.error(f"vision analysis failed for tool={canonical_name!r}", exc_info=True)
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
        if typed_route:
            logger.debug(f"routing to typed tool gateway: {canonical_name!r}")
            return typed_result(typed_tools, canonical_name, canonical_arguments, context, selected)
        if canonical_name in _INSPECTION_NAMES:
            logger.debug(f"routing to inspection service: {canonical_name!r}")
            result = application.inspections.inspect(
                canonical_name, canonical_arguments, context
            )
        else:
            logger.debug(f"routing to packaged tool executor: {canonical_name!r}")
            result = application.tool_executor.execute(
                ToolCall(tool=canonical_name, arguments=canonical_arguments), context
            )
        return {
            "output": result.output,
            "isError": not result.ok,
            "error": result.error_code,
            "evidence": dict(result.evidence or {}),
        }

    def execute(name: str, arguments: dict) -> dict:
        if discovery is None or name in {"tool_search", "tool_schema"}:
            return execute_inner(name, arguments)
        # Each call retains the selection visible when it started. A later
        # schema load cannot rewrite its audit record or returned identity.
        selected = visible
        canonical_name = _LEGACY_ALIASES.get(name, name)
        typed_tools = getattr(application, "tools", None)
        typed_admitted = (canonical_name in _TYPED_TOOL_NAMES and typed_tools is not None
                          and selected.allows(name))
        if typed_admitted:
            from ..domain.common.errors import InvalidInput
            try:
                validate_tool_call(registry.get(name), RegistryToolCall(
                    tool_name=name, arguments=dict(arguments)))
            except (InvalidInput, TypeError, ValueError):
                # A native preflight refusal never reached the typed gateway's
                # receipt boundary; record it in the compatibility audit.
                typed_admitted = False
        if typed_admitted:
            result = execute_inner(name, arguments, selected=selected)
        else:
            started = time.monotonic()
            context = local_owner_context(
                correlation_id=uuid.uuid4().hex, source="mcp", workspace_roots=roots,
                timeout_seconds=60.0,
            )
            try:
                result = execute_inner(name, arguments, selected=selected,
                                       operation_context=context)
            except Exception as exc:
                failed = {"output": "", "isError": True, "error": type(exc).__name__,
                          "evidence": {}}
                try:
                    audit_native(name, arguments, failed, context, selected,
                                 policy_path="native_mcp_compatibility", started=started)
                except Exception as audit_error:
                    raise McpTransportError(
                        "native tool audit unavailable; reconcile outcome before retry"
                    ) from audit_error
                raise
            try:
                audit_native(name, arguments, result, context, selected,
                             policy_path="native_mcp_compatibility", started=started)
            except Exception as audit_error:
                raise McpTransportError(
                    "native tool audit unavailable; reconcile outcome before retry"
                ) from audit_error
        evidence = dict(result.get("evidence") or {})
        evidence.update({"tool_schema_selection": selected.marker(),
                         "inventory_digest": discovery.digest})
        if not typed_admitted:
            evidence["native_compatibility_path"] = True
        return {**result, "evidence": evidence}

    logger.info(f"native MCP server serving, tool_count={len(registry.list_all())}, capabilities={capabilities!r}")
    logger.debug("starting stdio MCP transport")
    transport = StdioMcpTransport(
        input_stream or sys.stdin,
        output_stream or sys.stdout,
        compatibility=McpCompatibility(
            server_version="2.0",
            supported_versions=SUPPORTED_MCP_PROTOCOL_VERSIONS,
            capabilities=capabilities,
        ),
        tool_catalog=discovery_tools if discovery is not None else registry,
        tool_handler=execute,
        task_handler=task_handler,
        server_info_version=runtime_version(),
    )
    try:
        return transport.serve()
    finally:
        if close_compute_on_exit:
            try:
                close_delegation = getattr(application, "close_delegation", None)
                if callable(close_delegation):
                    close_delegation(timeout=5)
            finally:
                close_compute = getattr(application, "close_compute", None)
                if callable(close_compute):
                    close_compute()


__all__ = ["native_tool_registry", "run_native_mcp"]
