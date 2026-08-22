"""Offline API-003 proof for a real stdio MCP provider subprocess."""

import json
import os
import sys
from pathlib import Path

import pytest

from sonder_runtime.adapters.mcp_subprocess import McpProviderTimeout, McpSubprocessProvider
from sonder_runtime.adapters.mcp_subprocess import McpProviderCancelled
from sonder_runtime.application.protocol.mcp_compatibility import LegacyMcpContract
from sonder_runtime.application.jobs.durable_registry import ProcessTreeCleanupReceipt
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

_PROVIDER = REPO_ROOT / "tests" / "fixtures" / "api003_provider.py"
_PROVIDER_ENV = {
    **os.environ,
    "PYTHONPATH": str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
}


def _frame(request_id, method, params=None):
    return json.dumps({
        "jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {},
    }, separators=(",", ":")) + "\n"


def _start_provider():
    return McpSubprocessProvider(
        [sys.executable, str(_PROVIDER)],
        cwd=REPO_ROOT,
        env=_PROVIDER_ENV,
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
        [sys.executable, str(_PROVIDER), "--notifications"],
        cwd=REPO_ROOT,
        env=_PROVIDER_ENV,
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


def test_subprocess_composition_carries_explicit_declaration_and_cancellation():
    declaration = LegacyMcpContract("separate-provider", "1.0", ("tools",))
    provider = McpSubprocessProvider(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=REPO_ROOT, declaration=declaration, timeout_seconds=5,
    )
    checks = iter((False, True))
    with pytest.raises(McpProviderCancelled, match="cancelled"):
        provider.run("", cancel_check=lambda: next(checks, True))
    assert provider.declaration is declaration
    assert provider.cleanup_receipt is not None
    assert provider.cleanup_receipt.complete is False


def test_subprocess_timeout_preserves_incomplete_cleanup_receipt():
    class IncompleteCleanup:
        def cleanup(self, request):
            return ProcessTreeCleanupReceipt(
                request.job_id, True, descendants_seen=2,
                descendants_terminated=1, complete=False,
                detail="descendant could not be verified",
            )

    provider = McpSubprocessProvider(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=REPO_ROOT, timeout_seconds=0.2, cleanup=IncompleteCleanup(),
    )
    with pytest.raises(McpProviderTimeout):
        provider.run("")
    assert provider.cleanup_receipt is not None
    assert provider.cleanup_receipt.complete is False
    assert "could not be verified" in provider.cleanup_receipt.detail


def test_new_provider_instance_can_restart_after_previous_bounded_timeout():
    first = McpSubprocessProvider(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=REPO_ROOT, timeout_seconds=0.2,
    )
    with pytest.raises(McpProviderTimeout):
        first.run("")
    second = McpSubprocessProvider(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], cwd=REPO_ROOT,
    )
    assert second.run("") == ("", "")


def test_production_composition_rejects_invalid_configuration_before_launch():
    with pytest.raises(ValueError, match="argv"):
        McpSubprocessProviderConfig(argv=())
    with pytest.raises(ValueError, match="environment"):
        McpSubprocessProviderConfig(argv=("provider",), env={"TOKEN": None})
    with pytest.raises(ValueError, match="timeouts"):
        McpSubprocessProviderConfig(argv=("provider",), timeout_seconds=0)
