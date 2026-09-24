import io
import json
from types import SimpleNamespace

import pytest

from sonder_runtime.application.ports.tool_registry import (
    InMemoryToolRegistry,
    ToolDescriptor,
)
from sonder_runtime.application.tools.discovery import ToolDiscovery


def discovery():
    return ToolDiscovery(InMemoryToolRegistry((
        ToolDescriptor("file_read", "Read a guarded file", {"type": "object"}),
        ToolDescriptor("file_write", "Write a guarded file", {"type": "object"}),
        ToolDescriptor("hidden", "secret administrative schema", {"type": "object"}),
    )), allowed_names=("file_read", "file_write"))


def test_search_returns_bounded_summaries_and_load_does_not_widen_grants():
    service = discovery()
    result = service.search("read file", limit=1)
    assert result["matches"] == [{"name": "file_read", "summary": "Read a guarded file"}]
    assert result["truncated"]
    assert "hidden" not in json.dumps(service.search(""))
    selection, payload = service.load(["file_read"], inventory_digest=service.digest, selection_id="turn-1")
    assert selection.visible_names == frozenset({"file_read"})
    assert [x["name"] for x in payload["tools"]] == ["file_read"]
    assert payload == service.load(["file_read"], inventory_digest=service.digest, selection_id="turn-1")[1]
    payload["tools"][0]["inputSchema"]["malicious"] = True
    assert "malicious" not in service.load(["file_read"], inventory_digest=service.digest,
                                          selection_id="turn-1")[1]["tools"][0]["inputSchema"]
    with pytest.raises(ValueError, match="grant"):
        service.load(["hidden"], inventory_digest=service.digest, selection_id="turn-1")


def test_stale_inventory_and_unbounded_load_fail_closed():
    service = discovery()
    with pytest.raises(ValueError, match="changed"):
        service.load(["file_read"], inventory_digest="stale", selection_id="turn-1")
    with pytest.raises(ValueError, match="unique"):
        service.load(["file_read", "file_read"], inventory_digest=service.digest, selection_id="turn-1")
    with pytest.raises(ValueError, match="512"):
        service.search("x" * 513)


def test_selection_identity_survives_durable_audit_reopen(tmp_path):
    from sonder_runtime.adapters.persistence.tool_audit import (
        DurableToolAuditRepository,
    )
    from sonder_runtime.application.tools.gateway_contract import (
        ToolGatewayRequest,
        ToolPermission,
        ToolReceipt,
        ToolScope,
    )
    service = discovery()
    selected, payload = service.load(["file_read"], inventory_digest=service.digest, selection_id="turn-1")
    path = tmp_path / "audit.jsonl"
    request = ToolGatewayRequest(request_id="r1", tool_name="file_read", arguments={},
                                 scope=ToolScope("owner"), permission=ToolPermission(),
                                 schema_selection=selected)
    receipt = ToolReceipt(request_id="r1", tool_name="file_read", success=True, output="ok")
    DurableToolAuditRepository(path).append(request, receipt)
    restored = DurableToolAuditRepository(path).read()[0]
    assert restored["tool_schema_selection"]["visible_names"] == ["file_read"]
    assert restored["tool_schema_selection"]["selection_id"] == payload["manifest"]["selection"]["selection_id"]


def test_native_progressive_transport_refuses_hidden_tools_until_schema_load():
    from sonder_runtime.application.ports.tool_executor import ToolResult
    from sonder_runtime.bootstrap.native_mcp import native_tool_registry, run_native_mcp

    digest = ToolDiscovery(native_tool_registry()).digest
    messages = [
        ("initialize", {"protocolVersion": "2.0", "capabilities": {}}),
        ("tools/list", {}),
        ("tools/call", {"name": "process_list", "arguments": {}}),
        ("tools/call", {"name": "tool_search", "arguments": {"query": "process"}}),
        ("tools/call", {"name": "tool_schema", "arguments": {
            "names": ["process_list"], "inventory_digest": digest}}),
        ("tools/call", {"name": "process_list", "arguments": {}}),
        ("tools/call", {"name": "file_read", "arguments": {"path": "x"}}),
    ]
    stream = io.StringIO("".join(json.dumps({"jsonrpc": "2.0", "id": i, "method": name, "params": params}) + "\n"
                                for i, (name, params) in enumerate(messages)))
    output = io.StringIO()
    class Executor:
        def execute(self, call, context):
            return ToolResult(ok=True, output=call.tool + ":ok")
    app = SimpleNamespace(config=None, tool_executor=Executor())
    run_native_mcp(app, input_stream=stream, output_stream=output, progressive_tools=True)
    replies = [json.loads(row) for row in output.getvalue().splitlines()]
    assert {x["name"] for x in replies[1]["result"]["tools"]} == {"tool_search", "tool_schema"}
    assert replies[2]["result"]["error"] == "tool_not_visible"
    assert "inputSchema" not in replies[3]["result"]["output"]
    assert replies[5]["result"]["output"] == "process_list:ok"
    assert replies[6]["result"]["error"] == "tool_not_visible"
