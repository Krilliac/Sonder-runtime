from __future__ import annotations

import io
import json

from sonder_runtime.bootstrap.app import Application
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


def test_native_catalog_is_bounded_and_deterministic():
    assert [item.name for item in native_tool_registry().list_all()] == [
        "edit_file", "make_directory", "read_file", "run_program", "run_script", "write_file",
    ]


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
