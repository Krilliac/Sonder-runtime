import io
import json

import pytest

from sonder_runtime.application.protocol.mcp_compatibility import (
    LegacyMcpContract,
    McpCompatibility,
    SubscriptionNotificationRouter,
)
from sonder_runtime.interfaces.mcp.transport import McpTransportError, McpTransportLimits, StdioMcpTransport


def _transport(lines, *, handler=lambda name, args: {"output": {"name": name, "args": args}}, router=None, limits=None):
    output = io.StringIO()
    transport = StdioMcpTransport(
        io.StringIO("\n".join(json.dumps(line) for line in lines) + "\n"), output,
        compatibility=McpCompatibility(capabilities=("tools", "notifications")),
        tool_catalog=({"name": "echo", "description": "echo", "inputSchema": {"type": "object"}},),
        tool_handler=handler, notifications=router, limits=limits,
    )
    return transport, output


def test_initialize_lists_tools_calls_and_preserves_correlation():
    transport, output = _transport([
        {"jsonrpc": "2.0", "id": "i", "method": "initialize", "params": {"protocolVersions": ["1.0", "2.0"], "capabilities": {"tools": {}}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"x": 1}}},
    ])
    assert transport.serve() == 3
    result = [json.loads(line) for line in output.getvalue().splitlines()]
    assert result[0]["result"]["protocolVersion"] == "2.0"
    assert result[1]["id"] == 2 and result[1]["result"]["tools"][0]["name"] == "echo"
    assert result[2]["id"] == 3 and result[2]["result"]["output"]["args"] == {"x": 1}


def test_malformed_and_uninitialized_requests_are_bounded_errors():
    transport, output = _transport([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        {"jsonrpc": "1.0", "id": 2, "method": "initialize", "params": {}},
    ])
    transport.serve()
    result = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [item["error"]["code"] for item in result] == [-32602, -32600]


def test_malformed_json_frame_returns_parse_error_and_stream_continues():
    output = io.StringIO()
    transport = StdioMcpTransport(
        io.StringIO('{"broken"\n{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2.0"}}\n'),
        output, compatibility=McpCompatibility(), tool_catalog=(), tool_handler=lambda *_: {},
    )
    assert transport.serve() == 2
    result = [json.loads(line) for line in output.getvalue().splitlines()]
    assert result[0]["error"]["code"] == -32700
    assert result[1]["id"] == 1 and result[1]["result"]["protocolVersion"] == "2.0"


def test_subscription_router_emits_notification_and_request_ids_are_preserved():
    router = SubscriptionNotificationRouter()
    output = io.StringIO()
    transport = StdioMcpTransport(
        io.StringIO("\n".join(json.dumps(line) for line in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2.0", "capabilities": {"notifications": {}}}},
            {"jsonrpc": "2.0", "id": 2, "method": "sonder/subscribe", "params": {"event": "job.updated"}},
        ]) + "\n"), output, compatibility=McpCompatibility(capabilities=("notifications",)),
        tool_catalog=(), tool_handler=lambda *_: {}, notifications=router,
    )
    transport._write(transport._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2.0", "capabilities": {"notifications": {}}}}))
    transport._write(transport._dispatch({"jsonrpc": "2.0", "id": 2, "method": "sonder/subscribe", "params": {"event": "job.updated"}}))
    router.publish("job.updated", {"id": "j1"})
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert messages[1]["id"] == 2 and messages[1]["result"] == {"subscribed": "job.updated"}
    assert messages[2]["method"] == "notifications/event"
    assert messages[2]["params"]["payload"] == {"id": "j1"}


def test_eof_is_clean_and_oversized_frame_is_rejected():
    transport = StdioMcpTransport(io.StringIO(""), io.StringIO(), compatibility=McpCompatibility(), tool_catalog=(), tool_handler=lambda *_: {}, limits=McpTransportLimits(max_frame_bytes=8))
    assert transport.serve() == 0
    with pytest.raises(McpTransportError, match="max_frame_bytes"):
        transport._decode_frame('{"jsonrpc":"2.0"}')


def test_generated_catalog_registry_projection_is_supported():
    from sonder_runtime.application.ports.tool_registry import InMemoryToolRegistry, ToolDescriptor
    registry = InMemoryToolRegistry([ToolDescriptor("echo", input_schema={"type": "object"})])
    output = io.StringIO()
    transport = StdioMcpTransport(io.StringIO(""), output, compatibility=McpCompatibility(), tool_catalog=registry, tool_handler=lambda *_: {})
    assert transport._catalog[0]["name"] == "echo"


def test_transport_threads_registered_legacy_declaration_into_negotiation():
    legacy = LegacyMcpContract("legacy-server", "1.0", ("tools",))
    output = io.StringIO()
    transport = StdioMcpTransport(
        io.StringIO(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersions": ["1.0"], "capabilities": {"tools": {}}},
        }) + "\n"),
        output,
        compatibility=McpCompatibility(
            supported_versions=("2.0",),
            legacy_contracts=(legacy,),
            capabilities=("tools",),
        ),
        legacy_contract=legacy,
        tool_catalog=(), tool_handler=lambda *_: {},
    )

    assert transport.serve() == 1
    assert transport.negotiation is not None
    assert transport.negotiation.agreed_version == "1.0"
    assert transport.negotiation.legacy_contract == legacy


def test_transport_rejects_unregistered_legacy_declaration_fail_closed():
    legacy = LegacyMcpContract("legacy-server", "1.0")
    with pytest.raises(McpTransportError, match="not registered"):
        StdioMcpTransport(
            io.StringIO(""), io.StringIO(),
            compatibility=McpCompatibility(), legacy_contract=legacy,
            tool_catalog=(), tool_handler=lambda *_: {},
        )


def test_transport_without_legacy_declaration_does_not_downgrade():
    transport, output = _transport([{
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersions": ["1.0"]},
    }])
    transport.serve()
    result = json.loads(output.getvalue())
    assert result["error"]["code"] == -32001
