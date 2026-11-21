"""Native MCP forwards explicit location consent to the location adapter.

``approximate_location_lookup`` requires ``consent`` in its native schema, and
the packaged location adapter requires the same explicit ``consent=True`` in
addition to the context's cloud consent.  The native dispatcher consumed the
argument to derive ``cloud_allowed`` and never forwarded it, so the tool
refused every call ("explicit location and cloud consent are required") even
when the caller consented.
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from sonder_runtime.application.ports.tool_executor import ToolResult
from sonder_runtime.bootstrap.native_mcp import run_native_mcp


class _RecordingExecutor:
    def __init__(self):
        self.calls = []

    def execute(self, call, context):
        self.calls.append((call.tool, dict(call.arguments), context.cloud_allowed))
        return ToolResult(ok=True, output="recorded")


def _call(tmp_path, name, arguments):
    executor = _RecordingExecutor()
    app = SimpleNamespace(
        config=SimpleNamespace(state=SimpleNamespace(workspace_roots=(tmp_path,))),
        tool_executor=executor,
    )
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}},
    ]
    output = io.StringIO()
    run_native_mcp(app, input_stream=io.StringIO("\n".join(map(json.dumps, messages)) + "\n"),
                   output_stream=output)
    reply = [json.loads(line) for line in output.getvalue().splitlines()][-1]
    return executor.calls, reply


@pytest.mark.parametrize("consent", [True, False])
def test_location_lookup_receives_the_callers_explicit_consent(tmp_path, consent):
    calls, reply = _call(tmp_path, "approximate_location_lookup", {"consent": consent})

    assert "error" not in reply
    assert calls == [("approximate_location_lookup", {"consent": consent}, consent)]


def test_other_web_tools_still_take_consent_only_as_cloud_permission(tmp_path):
    # web_fetch/web_search/weather_lookup adapters take no ``consent`` keyword;
    # forwarding it would fail on their signatures.
    calls, reply = _call(tmp_path, "weather_lookup", {"location": "Paris", "consent": True})

    assert "error" not in reply
    assert calls == [("weather_lookup", {"location": "Paris"}, True)]


def test_cloud_consent_keeps_the_calls_deadline_and_cancellation(monkeypatch, tmp_path):
    # Consent widens only cloud permission. Rebuilding a fresh owner context
    # dropped the call's cancellation token and restarted its deadline.
    from sonder_runtime.bootstrap import native_mcp

    built = []
    original = native_mcp.local_owner_context

    def recording(**kwargs):
        context = original(**kwargs)
        built.append(context)
        return context

    monkeypatch.setattr(native_mcp, "local_owner_context", recording)
    seen = []

    class _ContextExecutor:
        def execute(self, call, context):
            seen.append(context)
            return ToolResult(ok=True, output="recorded")

    app = SimpleNamespace(
        config=SimpleNamespace(state=SimpleNamespace(workspace_roots=(tmp_path,))),
        tool_executor=_ContextExecutor(),
    )
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "web_search", "arguments": {"query": "x", "consent": True}}},
    ]
    run_native_mcp(app, input_stream=io.StringIO("\n".join(map(json.dumps, messages)) + "\n"),
                   output_stream=io.StringIO())

    assert len(seen) == 1 and seen[0].cloud_allowed is True
    origin = built[0]
    assert seen[0].cancellation is origin.cancellation
    assert seen[0].deadline_monotonic == origin.deadline_monotonic
    assert seen[0].correlation_id == origin.correlation_id
