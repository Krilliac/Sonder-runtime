from __future__ import annotations

import io
import json
from types import SimpleNamespace

from sonder_runtime.platform.config import SonderConfig
from sonder_runtime.bootstrap.native_mcp import native_tool_registry, run_native_mcp


class _Executor:
    def execute(self, call, context):
        from sonder_runtime.application.ports.tool_executor import ToolResult
        assert context.source == "mcp"
        return ToolResult(ok=True, output=call.tool + ":ok", evidence={"tool": call.tool})


def _app():
    class _State:
        workspace_roots = ()

    class _Config:
        state = _State()

    return type("App", (), {"config": _Config(), "tool_executor": _Executor()})()


class _Inspections:
    def inspect(self, name, arguments, context):
        from sonder_runtime.application.ports.tool_executor import ToolResult
        assert context.source == "mcp"
        return ToolResult(ok=True, output=name + ":inspection", evidence={"args": arguments})


def test_native_catalog_is_bounded_and_deterministic():
    assert [item.name for item in native_tool_registry().list_all()] == [
        "archive_list", "artifact_risk_inspect", "data_inspect", "data_query", "dependency_inventory",
        "directory_create", "directory_digest", "directory_tree", "edit_file",
        "file_batch_write", "file_copy", "file_delete", "file_digest", "file_find",
        "file_move", "file_read", "file_read_range", "file_write", "image_inspect",
        "json_patch", "log_inspect", "make_directory", "process_list", "process_memory_risk_inspect",
        "program_search", "project_detect", "read_file", "run_program", "run_script", "script_search", "text_patch", "text_search",
        "workspace_compare", "workspace_run", "write_file",
    ]


def test_native_catalog_contains_legacy_filesystem_alias_schemas():
    registry = native_tool_registry()
    read = registry.require("file_read")
    write = registry.require("file_write")
    run = registry.require("workspace_run")
    assert read.input_schema["required"] == ["path"]
    assert write.input_schema["properties"]["mode"]["enum"] == [
        "create", "overwrite", "append",
    ]
    assert run.input_schema["properties"]["program"]["type"] == "string"


def test_native_transport_calls_application_tool_port():
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}},
    }
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "read_file", "arguments": {"path": "x.txt"}},
    }
    output = io.StringIO()
    count = run_native_mcp(
        _app(), input_stream=io.StringIO(json.dumps(request) + "\n" + json.dumps(call) + "\n"),
        output_stream=output,
    )
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert count == 2
    assert rows[1]["result"]["output"] == "read_file:ok"
    assert rows[1]["result"]["isError"] is False


def test_native_legacy_file_read_alias_calls_canonical_executor():
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}},
    }
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "file_read", "arguments": {"path": "x.txt"}},
    }
    output = io.StringIO()
    run_native_mcp(
        _app(), input_stream=io.StringIO(json.dumps(request) + "\n" + json.dumps(call) + "\n"),
        output_stream=output,
    )
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[1]["result"]["output"] == "read_file:ok"


def test_native_schema_rejects_unknown_arguments_as_protocol_error():
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}},
    }
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "file_read", "arguments": {"path": "x.txt", "token": "secret"}},
    }
    output = io.StringIO()
    run_native_mcp(
        _app(), input_stream=io.StringIO(json.dumps(request) + "\n" + json.dumps(call) + "\n"),
        output_stream=output,
    )
    row = [json.loads(line) for line in output.getvalue().splitlines()][1]
    assert row["error"]["code"] == -32602


def test_native_read_only_inspection_routes_through_application_service():
    app = _app()
    app.inspections = _Inspections()
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}},
    }
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "file_digest", "arguments": {"path": "x.txt"}},
    }
    output = io.StringIO()
    run_native_mcp(
        app, input_stream=io.StringIO(json.dumps(request) + "\n" + json.dumps(call) + "\n"),
        output_stream=output,
    )
    row = [json.loads(line) for line in output.getvalue().splitlines()][1]
    assert row["result"]["output"] == "file_digest:inspection"


def test_native_entrypoint_fences_safety_before_configuration(monkeypatch):
    import sonder_runtime.__main__ as entrypoint
    import sonder_runtime.adapters.security.unsafe_lab as unsafe_lab
    import sonder_runtime.bootstrap.app as bootstrap_app
    import sonder_runtime.bootstrap.native_mcp as native_mcp

    calls = []
    monkeypatch.setattr(unsafe_lab, "require_startup", lambda: calls.append("safety"))
    monkeypatch.setattr(
        entrypoint, "_load_config", lambda args: calls.append("config") or SonderConfig()
    )
    monkeypatch.setattr(
        bootstrap_app, "build_application",
        lambda **kwargs: calls.append("build") or _app(),
    )
    monkeypatch.setattr(
        native_mcp, "run_native_mcp", lambda application: calls.append("run") or 0,
    )

    assert entrypoint.cmd_mcp(SimpleNamespace(native=True)) == 0
    assert calls == ["safety", "config", "build", "run"]
