"""Offline API-003 proof for a real stdio MCP provider subprocess."""

import json
import sys
from pathlib import Path

import pytest

from sonder_runtime.adapters.mcp_subprocess import McpProviderTimeout, McpSubprocessProvider
from sonder_runtime.bootstrap.subprocess_mcp import (
    McpSubprocessProviderConfig,
    build_mcp_subprocess_exchange,
)
from sonder_runtime.interfaces.mcp.transport import (
    BoundedMcpProviderExchange,
    McpTransportError,
    McpTransportLimits,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

_SERVER = r'''
import sys
from sonder_runtime.application.protocol.mcp_compatibility import McpCompatibility
from sonder_runtime.interfaces.mcp.transport import McpTransportLimits, StdioMcpTransport

def handle(name, arguments):
    if name == "echo":
        return {"output": {"echo": arguments.get("value")}}
    if name == "hang":
        import time
        time.sleep(30)
        return {"output": "unreachable"}
    raise KeyError(name)

transport = StdioMcpTransport(
    sys.stdin,
    sys.stdout,
    compatibility=McpCompatibility(
        server_version="2.0",
        supported_versions=("2.0",),
        capabilities=("tools",),
    ),
    tool_catalog=(
        {"name": "echo", "description": "local proof tool", "inputSchema": {"type": "object"}},
        {"name": "hang", "description": "termination proof tool", "inputSchema": {"type": "object"}},
    ),
    tool_handler=handle,
    limits=McpTransportLimits(max_frame_bytes=4096, max_arguments_bytes=64),
)
transport.serve()
'''

_NOTIFICATION_SERVER = r'''
import sys
from sonder_runtime.application.protocol.mcp_compatibility import McpCompatibility, SubscriptionNotificationRouter
from sonder_runtime.interfaces.mcp.transport import McpTransportLimits, StdioMcpTransport

router = SubscriptionNotificationRouter()
def handle(name, arguments):
    router.publish("progress", {"value": arguments.get("value")})
    return {"output": {"echo": arguments.get("value")}}

transport = StdioMcpTransport(
    sys.stdin, sys.stdout,
    compatibility=McpCompatibility(server_version="2.0", supported_versions=("2.0",), capabilities=("tools",)),
    tool_catalog=({"name": "echo", "description": "notification proof", "inputSchema": {"type": "object"}},),
    tool_handler=handle, notifications=router, connection_id="provider-test",
    limits=McpTransportLimits(max_frame_bytes=4096),
)
transport.serve()
'''


def _frame(request_id, method, params=None):
    return json.dumps({
        "jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {},
    }, separators=(",", ":")) + "\n"


def _start_provider():
    return McpSubprocessProvider(
        [sys.executable, "-c", _SERVER],
        cwd=REPO_ROOT,
        timeout_seconds=2,
    )


def test_subprocess_provider_negotiates_calls_and_rejects_oversized_arguments():
    provider = _start_provider()
    request = "".join([
        _frame("init", "initialize", {
            "protocolVersions": ["2.0"], "capabilities": {"tools": {}},
        }),
        _frame("list", "tools/list"),
        _frame("large", "tools/call", {
            "name": "echo", "arguments": {"value": "x" * 128},
        }),
        _frame("call", "tools/call", {
            "name": "echo", "arguments": {"value": "bounded"},
        }),
    ])
    stdout, stderr = provider.run(request)

    assert stderr == ""
    responses = [json.loads(line) for line in stdout.splitlines()]
    assert responses[0]["result"]["protocolVersion"] == "2.0"
    assert {tool["name"] for tool in responses[1]["result"]["tools"]} == {"echo", "hang"}
    assert responses[2]["id"] == "large"
    assert responses[2]["error"]["code"] == -32602
    assert "max_arguments_bytes" in responses[2]["error"]["message"]
    assert responses[3]["result"]["output"] == {"echo": "bounded"}


def test_subprocess_provider_is_terminated_on_bounded_call_timeout():
    provider = _start_provider()
    request = "".join([
        _frame(1, "initialize", {"protocolVersion": "2.0"}),
        _frame(2, "tools/call", {"name": "hang", "arguments": {}}),
    ])
    with pytest.raises(McpProviderTimeout, match="bounded call"):
        provider.run(request)


def test_subprocess_provider_notifies_start_exit_and_reaps_process():
    events = []
    provider = McpSubprocessProvider(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        cwd=REPO_ROOT,
        observer=events.append,
    )

    provider.run("")

    assert [event.state for event in events] == ["started", "exited"]
    assert events[-1].returncode == 0


def test_subprocess_provider_forwards_negotiated_subscription_notifications():
    provider = McpSubprocessProvider(
        [sys.executable, "-c", _NOTIFICATION_SERVER],
        cwd=REPO_ROOT,
        timeout_seconds=2,
    )
    request = "".join([
        _frame(1, "initialize", {"protocolVersions": ["2.0"], "capabilities": {"tools": {}}}),
        _frame(2, "sonder/subscribe", {"event": "progress"}),
        _frame(3, "tools/call", {"name": "echo", "arguments": {"value": "v"}}),
    ])
    stdout, stderr = provider.run(request)

    assert stderr == ""
    messages = [json.loads(line) for line in stdout.splitlines()]
    notifications = [item for item in messages if item.get("method") == "notifications/event"]
    assert notifications == [{
        "jsonrpc": "2.0", "method": "notifications/event",
        "params": {"event": "progress", "payload": {"value": "v"}},
    }]
    assert messages[-1]["id"] == 3


def test_provider_exchange_enforces_bounded_request_and_response_bytes():
    class Provider:
        def run(self, request):
            return request, ""

    exchange = BoundedMcpProviderExchange(
        Provider(), limits=McpTransportLimits(max_exchange_bytes=4)
    )
    assert exchange.exchange("1234")[0] == "1234"
    with pytest.raises(McpTransportError, match="request exceeds"):
        exchange.exchange("12345")


def test_production_composition_builds_bounded_subprocess_exchange():
    events = []
    exchange = build_mcp_subprocess_exchange(
        McpSubprocessProviderConfig(
            argv=(sys.executable, "-c", "import sys; sys.stdin.read()"),
            cwd=REPO_ROOT,
        ),
        limits=McpTransportLimits(max_exchange_bytes=8),
        observer=events.append,
    )

    assert exchange.exchange("1234") == ("", "")
    assert [event.state for event in events] == ["started", "exited"]


def test_production_composition_rejects_invalid_configuration_before_launch():
    with pytest.raises(ValueError, match="argv"):
        McpSubprocessProviderConfig(argv=())
    with pytest.raises(ValueError, match="environment"):
        McpSubprocessProviderConfig(argv=("provider",), env={"TOKEN": None})
    with pytest.raises(ValueError, match="timeouts"):
        McpSubprocessProviderConfig(argv=("provider",), timeout_seconds=0)
