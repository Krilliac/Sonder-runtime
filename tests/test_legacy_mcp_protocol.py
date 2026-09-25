"""Protocol contract of the legacy (``python -m sonder_runtime mcp``) surface.

The legacy tools report failures as ``ERROR:`` text, the upstream argument
models ignore unknown fields, and the upstream stdio transport reads frames of
any length and drops frames it cannot parse without answering. These tests
pin the boundary ``ReloadableMCPServer`` now puts in front of all of that:
failures carry ``isError``, unknown arguments are refused, frames are bounded,
and every malformed frame gets a JSON-RPC error instead of silence.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import reloadable_mcp
from reloadable_mcp import LEGACY_MCP_MAX_FRAME_BYTES, _frame_rejection

_SAMPLE_TOOLS = {"echo", "refuses"}

_SAMPLE_SOURCE = '''from reloadable_mcp import ReloadableMCPServer

existing = globals().get("_PERSISTENT_MCP")
if isinstance(existing, ReloadableMCPServer):
    mcp = existing
    mcp.begin_module_refresh()
else:
    mcp = ReloadableMCPServer("sample")
_PERSISTENT_MCP = mcp


@mcp.tool()
def echo(query: str, limit: int = 3) -> str:
    return "echo:%s:%d" % (query, limit)


@mcp.tool()
def refuses() -> str:
    return "ERROR: path is outside allowed roots"


mcp.finish_module_refresh(__name__, __file__, globals())

if __name__ == "__main__" and not globals().get("_MCP_HOT_RELOAD_EXEC"):
    mcp.run()
'''


@pytest.fixture
def allow_sample_tools(monkeypatch):
    import permission_modes

    monkeypatch.setattr(
        permission_modes,
        "_rule_lookup",
        lambda name: ({"action": permission_modes.ALLOW, "pattern": name}
                      if str(name or "").lstrip("/") in _SAMPLE_TOOLS else None),
    )


@pytest.fixture
def sample_mcp(monkeypatch, tmp_path, allow_sample_tools):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "0")
    module_name = "legacy_mcp_protocol_sample"
    (tmp_path / (module_name + ".py")).write_text(_SAMPLE_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        yield importlib.import_module(module_name).mcp
    finally:
        sys.modules.pop(module_name, None)


def _call(mcp, name, arguments):
    """Drive the protocol entry point, so ToolError becomes ``isError``."""
    from mcp.server.context import ServerRequestContext
    from mcp.types import CallToolRequestParams, LATEST_PROTOCOL_VERSION

    class _Session:
        async def send_tool_list_changed(self):
            return None

    ctx = ServerRequestContext(
        session=_Session(), lifespan_context=None,
        protocol_version=LATEST_PROTOCOL_VERSION, method="tools/call",
    )
    params = CallToolRequestParams(name=name, arguments=arguments)
    return asyncio.run(mcp._handle_call_tool(ctx, params))


def test_legacy_error_reply_is_an_mcp_tool_error(sample_mcp):
    result = _call(sample_mcp, "refuses", {})
    assert result.is_error is True
    assert result.content[0].text == "ERROR: path is outside allowed roots"


def test_legacy_success_reply_is_not_flagged(sample_mcp):
    result = _call(sample_mcp, "echo", {"query": "x"})
    assert result.is_error is False
    assert result.content[0].text == "echo:x:3"


def test_unknown_argument_is_refused_with_the_accepted_names(sample_mcp):
    result = _call(sample_mcp, "echo", {"query": "x", "bogus": 1})
    assert result.is_error is True
    text = result.content[0].text
    assert "bogus" in text and "limit, query" in text
    with pytest.raises(ToolError, match="does not accept argument"):
        asyncio.run(sample_mcp.call_tool("echo", {"query": "x", "limt": 9}))


def test_unknown_argument_is_refused_before_the_permission_gate(sample_mcp, monkeypatch):
    """A malformed call runs nothing, so it must never reach (or spend) the gate."""
    def exploding_gate(*_args, **_kwargs):
        raise AssertionError("the gate saw a call that should have been refused")

    monkeypatch.setattr(reloadable_mcp, "_refuse_if_gated", exploding_gate)
    with pytest.raises(ToolError, match="does not accept argument"):
        asyncio.run(sample_mcp.call_tool("echo", {"query": "x", "bogus": 1}))


def test_real_legacy_refusals_carry_is_error(monkeypatch):
    """The finding's own repros, on the real ``server.mcp`` surface."""
    import server
    from sonder_runtime.platform.version import runtime_version

    monkeypatch.setenv("SONDER_WEB_TOOLS", "0")
    web = asyncio.run(server.mcp.call_tool("web_fetch", {"url": "https://example.com"}))
    assert web.is_error is True
    assert web.content[0].text.startswith("ERROR: web tools disabled")
    with pytest.raises(ToolError, match="bogus"):
        asyncio.run(server.mcp.call_tool("memory_search", {"query": "x", "bogus": 1}))
    # serverInfo.version was empty: the server was built without a version.
    options = server.mcp._lowlevel_server.create_initialization_options()
    assert options.server_version == runtime_version()
    assert options.server_version


@pytest.mark.parametrize("frame, code, request_id", [
    (b"not json\n", -32700, None),
    (b"[1,2,3]\n", -32600, None),
    (b'{"jsonrpc":"2.0","id":4,"method":"ping"\n', -32700, None),
    (b'{"jsonrpc":"1.0","id":5,"method":"ping"}\n', -32600, 5),
    (b'{"jsonrpc":"2.0","id":true,"method":"ping"}\n', -32600, None),
    (b'{"jsonrpc":"2.0","id":null,"method":"ping"}\n', -32600, None),
    (b'{"jsonrpc":"2.0","id":7,"method":"tools/call","params":'
     b'{"name":"echo","arguments":{"query":"a\\ud800b"}}}\n', -32700, 7),
    (b'\xff\xfe\n', -32700, None),
])
def test_malformed_frames_are_classified(frame, code, request_id):
    rejection = _frame_rejection(frame)
    assert rejection is not None
    assert rejection[0] == request_id
    assert rejection[1] == code


def test_frame_bound_admits_every_call_a_legacy_tool_accepts():
    """A bound below a tool's own cap would refuse legitimate calls.

    ``file_batch_write`` takes up to MAX_BATCH_JSON_BYTES of JSON text, which
    grows by up to 3x when escaped into the JSON-RPC frame; a ``file_write``
    at MAX_WRITE_BYTES grows by up to 6x.
    """
    from sonder_runtime.adapters.filesystem import file_ops

    assert LEGACY_MCP_MAX_FRAME_BYTES > 3 * file_ops.MAX_BATCH_JSON_BYTES
    assert LEGACY_MCP_MAX_FRAME_BYTES > 6 * file_ops.MAX_WRITE_BYTES
    content = "\U0001F600" * (file_ops.MAX_BATCH_BYTES // 4 // 2)
    operations = json.dumps([
        {"path": "a.txt", "content": content, "mode": "create"},
        {"path": "b.txt", "content": content, "mode": "create"},
    ], ensure_ascii=False)
    assert len(operations.encode("utf-8")) <= file_ops.MAX_BATCH_JSON_BYTES
    frame = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "file_batch_write", "arguments": {"operations_json": operations},
    }}).encode("utf-8") + b"\n"
    assert len(frame) > 2 * 1_000_000 + 64 * 1024  # the bound this replaced
    assert len(frame) <= LEGACY_MCP_MAX_FRAME_BYTES
    assert _frame_rejection(frame) is None


def test_valid_frames_pass_through_unchanged():
    assert _frame_rejection(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n') is None
    assert _frame_rejection(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n') is None


def test_real_stdio_answers_every_malformed_frame_and_keeps_serving(tmp_path):
    """Upstream dropped these silently; a client waiting on the id hung.

    Also covers the frame bound: an oversized frame is refused and its tail is
    never read back as a second frame (a request smuggled past the bound would
    otherwise run).
    """
    server_path = tmp_path / "legacy_frames_server.py"
    server_path.write_text(_SAMPLE_SOURCE, encoding="utf-8")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sonder_home = tmp_path / "sonder_home"
    sonder_home.mkdir()
    smuggled = json.dumps({"jsonrpc": "2.0", "id": 66, "method": "ping"})
    frames = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        }}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "not json",
        "[1,2,3]",
        '{"jsonrpc":"2.0","id":4,"method":"ping"',
        json.dumps({"jsonrpc": "1.0", "id": 5, "method": "ping"}),
        '{"jsonrpc":"2.0","id":true,"method":"ping"}',
        '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":'
        '{"name":"echo","arguments":{"query":"a\\ud800b"}}}',
        "A" * (LEGACY_MCP_MAX_FRAME_BYTES + 1) + smuggled,
        json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}),
    ]
    completed = subprocess.run(
        [sys.executable, str(server_path)],
        input=("\n".join(frames) + "\n").encode("utf-8"),
        capture_output=True, cwd=str(tmp_path), timeout=240,
        env={**os.environ, "PYTHONPATH": repo_root, "SONDER_HOME": str(sonder_home),
             "SONDER_LIVE_RELOAD": "0"},
    )
    rows = [json.loads(line) for line in completed.stdout.decode("utf-8").splitlines() if line.strip()]
    assert rows[0]["id"] == 1 and "result" in rows[0], completed.stderr[-2000:]
    errors = [(row.get("id"), row["error"]["code"]) for row in rows[1:] if "error" in row]
    assert errors == [
        (None, -32700),  # not json
        (None, -32600),  # batch
        (None, -32700),  # truncated JSON
        (5, -32600),     # jsonrpc 1.0
        (None, -32600),  # boolean id
        (7, -32700),     # lone surrogate
        (None, -32600),  # oversized frame
    ]
    assert rows[-1] == {"jsonrpc": "2.0", "id": 9, "result": {}}
    assert all(row.get("id") != 66 for row in rows), "the oversized frame's tail ran"
