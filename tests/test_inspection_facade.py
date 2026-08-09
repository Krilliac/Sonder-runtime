"""Typed facade and compatibility contract for read-only inspections."""
from __future__ import annotations

import inspect
import os
from pathlib import Path
from types import SimpleNamespace

import server
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.inspection import INSPECTION_TOOLS, InspectionService
from sonder_runtime.application.ports.tool_executor import ToolCall, ToolResult
from sonder_runtime.adapters.legacy.inspections import LegacyInspectionExecutor


EXPECTED_TOOLS = {
    "archive_list", "data_inspect", "data_query", "dependency_inventory",
    "directory_digest", "file_digest", "log_inspect", "project_detect",
    "workspace_compare",
}


class CaptureExecutor:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or ToolResult(ok=True, output="inspection")

    def execute(self, call, context):
        self.calls.append((call, context))
        return self.result


def test_application_facade_is_exactly_read_only_and_typed():
    capture = CaptureExecutor(
        ToolResult(ok=True, output="ERROR: literal successful content")
    )
    service = InspectionService(capture)
    context = local_owner_context(correlation_id="inspection-test", source="mcp")

    result = service.inspect("log_inspect", {"path": "app.log"}, context)
    rejected = service.inspect("archive_extract", {"path": "app.zip"}, context)

    assert INSPECTION_TOOLS == EXPECTED_TOOLS
    assert result.ok is True
    assert result.output == "ERROR: literal successful content"
    assert capture.calls[0][0].tool == "log_inspect"
    assert capture.calls[0][1] is context
    assert rejected.ok is False
    assert rejected.error_code == "unknown_inspection"
    assert len(capture.calls) == 1


def test_legacy_adapter_maps_malformed_internal_calls_to_typed_failure():
    context = local_owner_context(correlation_id="malformed-inspection")
    result = LegacyInspectionExecutor().execute(
        ToolCall(tool="file_digest", arguments={}), context
    )
    assert result.ok is False
    assert result.error_code == "KeyError"


def test_server_bridge_carries_authorization_roots_and_preserves_typed_text(
    monkeypatch, tmp_path
):
    capture = SimpleNamespace()
    calls = []

    class Inspections:
        def inspect(self, name, arguments, context):
            capture.name = name
            capture.arguments = arguments
            capture.context = context
            return ToolResult(
                ok=True,
                output="ERROR: literal successful content",
                evidence={
                    "summary": "safe summary",
                    "audit_args": {"path": "app.log"},
                    "activity": {"summary": "one line", "path": "app.log"},
                },
            )

    monkeypatch.setattr(
        server, "_application", lambda: SimpleNamespace(inspections=Inspections())
    )
    monkeypatch.setattr(server, "_file_developer_allowed", lambda token: False)
    monkeypatch.setattr(server, "_file_bypass_allowed", lambda token, approval: True)
    monkeypatch.setattr(
        server, "_record_direct_tool",
        lambda name, args, **kwargs: calls.append((name, args, kwargs)),
    )
    events = []
    monkeypatch.setattr(
        server.activity_tracker,
        "record_event",
        lambda name, **kwargs: events.append((name, kwargs)),
    )

    output = server._run_read_only_inspection(
        "log_inspect", {"path": "app.log"}, approval="approved",
        extra_roots=os.pathsep.join((str(tmp_path), str(tmp_path / "other"))),
    )

    assert output == "ERROR: literal successful content"
    assert capture.name == "log_inspect"
    assert capture.context.source == "mcp"
    assert capture.context.auth_level == "user"
    assert capture.context.workspace_roots == (
        Path(tmp_path), Path(tmp_path / "other"),
    )
    assert calls[0][0:2] == ("log_inspect", {"path": "app.log"})
    assert calls[0][2]["ok"] is True
    assert events == [("log_inspect", {"summary": "one line", "path": "app.log"})]


def test_server_bridge_renders_typed_adapter_failure(monkeypatch):
    class Inspections:
        def inspect(self, name, arguments, context):
            return ToolResult(
                ok=False, output="path rejected", error_code="PermissionError",
                evidence={"summary": "path rejected", "audit_args": arguments},
            )

    monkeypatch.setattr(
        server, "_application", lambda: SimpleNamespace(inspections=Inspections())
    )
    monkeypatch.setattr(server, "_file_developer_allowed", lambda token: False)
    monkeypatch.setattr(server, "_file_bypass_allowed", lambda token, approval: False)
    monkeypatch.setattr(server, "_record_direct_tool", lambda *args, **kwargs: None)
    assert server._run_read_only_inspection(
        "data_inspect", {"path": "memory.db"}
    ) == "ERROR: path rejected"


def test_exact_mcp_signatures_and_policy_classifications_are_stable():
    expected_signatures = {
        "log_inspect": "(path: str, tail_lines: int = 0, context_lines: int = 2, max_file_bytes: int = 64000000, max_scan_bytes: int = 4000000, max_lines: int = 10000, max_line_bytes: int = 4096, max_results: int = 100, max_output_bytes: int = 256000, timeout: float = 5.0, token: str = '', approval: str = '', extra_roots: str = '') -> str",
        "workspace_compare": "(left: str, right: str, max_entries: int = 2000, max_file_bytes: int = 64000000, max_total_bytes: int = 256000000, max_details: int = 1000, max_output_bytes: int = 256000, timeout: float = 5.0, token: str = '', approval: str = '', extra_roots: str = '') -> str",
        "project_detect": "(path: str = '.', max_depth: int = 8, max_files: int = 200, max_total_bytes: int = 2000000, max_file_bytes: int = 256000, max_results: int = 500, token: str = '', approval: str = '', extra_roots: str = '') -> str",
        "file_digest": "(path: str, max_bytes: int = 32000000, token: str = '', approval: str = '', extra_roots: str = '') -> str",
        "directory_digest": "(path: str = '.', max_depth: int = 12, max_files: int = 2000, max_total_bytes: int = 32000000, max_file_bytes: int = 32000000, max_results: int = 2500, token: str = '', approval: str = '', extra_roots: str = '') -> str",
        "archive_list": "(path: str, max_entries: int = 2000, max_file_bytes: int = 64000000, max_total_bytes: int = 256000000, max_ratio: float = 100.0, max_path_depth: int = 32, max_results: int = 2500, max_seconds: float = 10.0, token: str = '', approval: str = '', extra_roots: str = '') -> str",
        "data_inspect": "(path: str, max_bytes: int = 256000, token: str = '', approval: str = '', extra_roots: str = '') -> str",
        "data_query": "(path: str, sql: str = '', projection_json: str = '[]', filters_json: str = '{}', max_rows: int = 100, max_columns: int = 50, max_output_bytes: int = 256000, max_scan_bytes: int = 4000000, timeout: float = 5.0, token: str = '', approval: str = '', extra_roots: str = '') -> str",
        "dependency_inventory": "(path: str = '.', max_depth: int = 5, max_files: int = 100, max_total_bytes: int = 2000000, max_results: int = 2000, token: str = '', approval: str = '', extra_roots: str = '') -> str",
    }
    assert {
        name: str(inspect.signature(getattr(server, name)))
        for name in EXPECTED_TOOLS
    } == expected_signatures
    for name in EXPECTED_TOOLS:
        assert name in server.REPOSITORY_READ_ONLY_TOOLS
        assert name in server._PROJECT_SCOPED_PATH_TOOLS
        assert name in server._PROJECT_BOUND_AGENT_TOOLS
        assert name in server._WORK_INSPECTION_TOOLS
        assert name in server._AGENT_DEDUPLICATED_INSPECTION_TOOLS
        assert name in server._AUTOPILOT_OBSERVE_TOOLS
        assert name not in server._WORK_MUTATION_TOOLS


def test_legacy_adapter_has_no_server_dependency_or_mutator_names():
    from sonder_runtime.adapters.legacy import inspections

    source = Path(inspections.__file__).read_text(encoding="utf-8")
    assert "import server" not in source
    assert "archive_extract" not in source
    assert "sqlite_mutate" not in source
    assert "write_file" not in source
