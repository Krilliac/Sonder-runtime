"""Native MCP honours a caller's ``extra_roots`` only under a spent approval.

On the typed route ``extra_roots`` widens reach only through ``reach_scope``
after a one-shot approval of exactly that call was spent. The non-typed route
forwarded the argument verbatim to the packaged executor, and several adapters
(``archive_create``, ``archive_extract``, ``artifact_risk_inspect``) resolve it
through ``resolve_repository_read_path``, which honours ``extra_roots`` with no
bypass check, so an unattended caller could widen its own filesystem reach.
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
        self.calls.append((call.tool, dict(call.arguments)))
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


_ARCHIVE_CREATE = {
    "root": "project", "inputs_json": "[\"a.txt\"]", "destination": "out.zip",
}


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    import permission_modes as pm
    from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger

    store = ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: store)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    return store


def test_unapproved_extra_roots_never_reach_the_packaged_executor(monkeypatch, tmp_path, ledger):
    import permission_modes as pm

    # ``auto`` lets the tool name through without any approval being spent.
    monkeypatch.setitem(pm._STATE, "mode", pm.AUTO)
    arguments = {**_ARCHIVE_CREATE, "extra_roots": str(tmp_path.anchor)}

    calls, reply = _call(tmp_path, "archive_create", arguments)

    assert reply["result"]["isError"] is False, reply
    assert calls == [("archive_create", _ARCHIVE_CREATE)]


def test_spent_one_shot_approval_keeps_exactly_the_approved_extra_roots(
        monkeypatch, tmp_path, ledger):
    import permission_modes as pm

    monkeypatch.setitem(pm._STATE, "mode", pm.MANUAL)
    arguments = {**_ARCHIVE_CREATE, "extra_roots": str(tmp_path / "outside")}
    issued = ledger.issue("archive_create", pm.call_digest("archive_create", arguments),
                          approver="console operator")

    calls, reply = _call(tmp_path, "archive_create", arguments)

    assert reply["result"]["isError"] is False, reply
    assert calls == [("archive_create", arguments)]
    assert ledger.get(issued.nonce).open() is False
