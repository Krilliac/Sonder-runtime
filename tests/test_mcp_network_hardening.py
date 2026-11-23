"""Regression tests for Wave 2 MCP, external MCP, extension host, dispatch, and inference hardening."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import urllib.error
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver import MCPServer

from sonder_runtime.adapters import embeddings as embeddings_mod
from sonder_runtime.adapters import external_mcp
from sonder_runtime.adapters.extensions.host import (
    ExtensionHost,
    ExtensionHostLimits,
    ExtensionHostProtocolError,
)
from sonder_runtime.adapters.inference import openai_compat_gateway as oai_mod
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.adapters.security import role_tool_policy
from sonder_runtime.adapters.tool_executor import ToolExecutorAdapter, _NETWORK_REFUSAL_TOOLS
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.tool_executor import ToolCall
from sonder_runtime.application.protocol.mcp_compatibility import McpCompatibility
from sonder_runtime.bootstrap import agent_help_tools
from sonder_runtime.bootstrap import generic_agent_dispatch as generic_dispatch
from sonder_runtime.bootstrap.native_mcp import native_tool_registry, run_native_mcp
from sonder_runtime.domain.common.errors import DependencyUnavailable, InvalidInput
from sonder_runtime.interfaces.mcp.transport import McpTransportLimits, StdioMcpTransport


# ---------------------------------------------------------------------------
# 1. MCP transport (NaN/Infinity, float IDs, allow_nan=False)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("frame", [
    '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":NaN}}',
    '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":Infinity}}',
    '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":-Infinity}}',
    '{"jsonrpc":"2.0","id":NaN,"method":"ping"}',
])
def test_mcp_transport_rejects_non_finite_json_constants(frame):
    ping = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})
    output = io.StringIO()
    transport = StdioMcpTransport(
        io.StringIO(frame + "\n" + ping + "\n"), output,
        compatibility=McpCompatibility(), tool_catalog=(), tool_handler=lambda *_: {},
    )
    assert transport.serve() == 2
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == -32700
    assert replies[0]["id"] is None
    assert replies[1] == {"jsonrpc": "2.0", "id": 9, "result": {}}


@pytest.mark.parametrize("bad_id", [1.5, 1.0, float("nan"), float("inf"), float("-inf"), True, False, [], {}])
def test_mcp_transport_rejects_float_and_non_finite_ids(bad_id):
    assert StdioMcpTransport._valid_id(bad_id) is False


def test_mcp_transport_dispatch_returns_null_id_for_float_id():
    output = io.StringIO()
    transport = StdioMcpTransport(
        io.StringIO('{"jsonrpc":"2.0","id":1.5,"method":"ping"}\n'), output,
        compatibility=McpCompatibility(), tool_catalog=(), tool_handler=lambda *_: {},
    )
    assert transport.serve() == 1
    reply = json.loads(output.getvalue())
    assert reply == {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}


@pytest.mark.parametrize("protocol_version", ["2.0", "2025-11-25"])
def test_mcp_transport_never_emits_nan_in_response(protocol_version):
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersions": [protocol_version]}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "bad", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ]
    output = io.StringIO()
    transport = StdioMcpTransport(
        io.StringIO("\n".join(json.dumps(f) for f in frames) + "\n"), output,
        compatibility=McpCompatibility(),
        tool_catalog=({"name": "bad", "inputSchema": {"type": "object"}},),
        tool_handler=lambda _name, _args: {"value": float("nan")},
    )
    assert transport.serve() == 3
    raw_lines = output.getvalue().splitlines()
    assert all("NaN" not in line and "Infinity" not in line for line in raw_lines)
    replies = [json.loads(line) for line in raw_lines]
    assert replies[1]["error"]["code"] == -32603
    assert replies[2] == {"jsonrpc": "2.0", "id": 3, "result": {}}


# ---------------------------------------------------------------------------
# 2. Generic agent dispatch (operator/recursive/role/web gates)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "learn_from_example", "sonder_remember_fact", "sonder_forget_fact", "playbook_note",
    "computer_task", "computer_use_start", "computer_use_stop", "screen_capture",
    "window_list", "ui_action",
    "batch_job_create", "batch_job_add_items", "batch_job_start",
    "batch_job_resume", "batch_job_cancel",
    "model_fanout", "model_fanout_resume", "model_fanout_cancel", "model_fanout_synthesize",
    "session_export", "permission_approvals",
])
def test_generic_dispatch_excludes_operator_and_recursive_tools_even_without_server_set(name):
    called = []
    reg = MCPServer("test")
    reg.tool(name=name)(lambda: called.append(name) or "ran")
    result = generic_dispatch.dispatch(name, {}, reg, (), run_refusal=lambda *a, **k: "")
    assert "HOST POLICY" in result and "excluded" in result
    assert called == []


def test_generic_dispatch_enforces_caller_role_policy():
    called = []
    reg = MCPServer("test")
    reg.tool(name="custom_priv_tool")(lambda: called.append(True) or "ran")
    with role_tool_policy.caller_role_scope("user"):
        result = generic_dispatch.dispatch(
            "custom_priv_tool", {}, reg, (), run_refusal=lambda *a, **k: "",
        )
    assert "HOST POLICY" in result
    assert called == []


@pytest.mark.parametrize("name", [
    "fetch_artifact", "local_service_probe", "openrouter_models", "openrouter_account",
])
def test_generic_dispatch_enforces_allow_web_false(name):
    called = []
    reg = MCPServer("test")
    reg.tool(name=name)(lambda: called.append(name) or "ran")
    result = generic_dispatch.dispatch(
        name, {}, reg, (), run_refusal=lambda *a, **k: "", allow_web=False,
    )
    assert "HOST POLICY" in result and "allow_web" in result
    assert called == []


# ---------------------------------------------------------------------------
# 3. Native MCP (compute_artifact_fetch cap, tool_help schema) & agent_help_tools
# ---------------------------------------------------------------------------

def test_native_compute_artifact_fetch_80k_fits_standard_mcp_frame(monkeypatch):
    from sonder_runtime.adapters.security.permission_policy import permission_policy

    monkeypatch.setattr(
        permission_policy,
        "decide_for_caller",
        lambda *_a, **_k: SimpleNamespace(action="allow"),
    )
    registry = native_tool_registry()
    schema = registry.require("compute_artifact_fetch").input_schema
    assert schema["properties"]["max_bytes"]["maximum"] == 80_000

    seen_max_bytes = []
    raw_bytes = b"A" * 80_000

    class _Compute:
        def fetch_artifact(self, job_id, name, *, max_bytes):
            seen_max_bytes.append(max_bytes)
            return SimpleNamespace(
                receipt=SimpleNamespace(
                    name=name, size_bytes=len(raw_bytes),
                    mime_type="application/octet-stream", sha256="b" * 64,
                ),
                content=raw_bytes,
            )

    app = SimpleNamespace(
        config=SimpleNamespace(state=SimpleNamespace(workspace_roots=())),
        compute_service=lambda: _Compute(),
        tools=None,
        tool_executor=None,
        inspections=None,
    )
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersions": ["2025-11-25"], "capabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "compute_artifact_fetch",
                    "arguments": {"controller_job_id": "job-1", "name": "blob.bin"}}},
    ]
    output = io.StringIO()
    run_native_mcp(
        app,
        input_stream=io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"),
        output_stream=output,
    )
    lines = output.getvalue().splitlines()
    assert len(lines[1].encode("utf-8")) <= McpTransportLimits().max_frame_bytes
    reply = json.loads(lines[1])
    assert "error" not in reply
    assert seen_max_bytes == [80_000]
    inner = json.loads(reply["result"]["structuredContent"]["output"])
    assert base64.b64decode(inner["content_base64"]) == raw_bytes


def test_native_tool_help_accepts_name_with_empty_query():
    schema = native_tool_registry().require("tool_help").input_schema
    assert "oneOf" not in schema

    app = SimpleNamespace(
        config=SimpleNamespace(state=SimpleNamespace(workspace_roots=())),
        tools=None,
        tool_executor=None,
        inspections=None,
    )
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersions": ["2.0"]}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "tool_help", "arguments": {"name": "file_read", "query": ""}}},
    ]
    output = io.StringIO()
    run_native_mcp(
        app,
        input_stream=io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"),
        output_stream=output,
    )
    reply = json.loads(output.getvalue().splitlines()[1])
    assert "error" not in reply
    assert reply["result"]["isError"] is False
    assert "file_read" in reply["result"]["output"]


def test_agent_help_checklist_update_item_id_alias_inherits_item_schema():
    import server

    schema = agent_help_tools.argument_schema(server.mcp, "checklist_update")
    assert schema is not None
    props = schema["properties"]
    assert props["item"]
    assert props["item_id"] == props["item"]


# ---------------------------------------------------------------------------
# 4. External MCP (row/block caps + ANSI/C0/bidi sanitization)
# ---------------------------------------------------------------------------

class _FakeEvents:
    def emit(self, *_args, **_kwargs):
        pass


class _StaticTransport:
    def __init__(self, result):
        self.result = result

    async def invoke(self, _request):
        return self.result


def _ext_bridge(result=None):
    server = external_mcp.ExternalMcpServerPolicy(
        name="docs",
        endpoint="http://127.0.0.1:8765/mcp",
        tools=(external_mcp.ExternalMcpToolPolicy("lookup"),),
    )
    return external_mcp.ExternalMcpBridge(
        (server,),
        transport=_StaticTransport(result),
        events=_FakeEvents(),
        secret_resolver=lambda _name: None,
    )


def test_external_mcp_caps_ingest_tool_list_rows():
    bridge = _ext_bridge()
    too_many = ({"name": f"t{i}"} for i in range(2049))
    with pytest.raises(external_mcp.ExternalMcpError) as exc_info:
        bridge.ingest_tool_list("docs", {"tools": too_many})
    assert exc_info.value.code == "INVALID_METADATA"


def test_external_mcp_caps_content_blocks():
    blocks = [{"type": "text", "text": "x"} for _ in range(257)]
    bridge = _ext_bridge({"content": blocks, "isError": False})
    ctx = local_owner_context(correlation_id="ext-cap", source="mcp", timeout_seconds=5)
    with pytest.raises(external_mcp.ExternalMcpError) as exc_info:
        asyncio.run(bridge.call("docs", "lookup", {}, context=ctx))
    assert exc_info.value.code == "RESULT_TOO_LARGE"


def test_external_mcp_sanitizes_ansi_c0_and_bidi_in_content_and_structured():
    dirty = "ok\x1b[31mRED\x1b[0m\x00\x07\r\u202eevil\u2066\u061c\n\tkept"
    ctx = local_owner_context(correlation_id="ext-san", source="mcp", timeout_seconds=5)

    bridge_text = _ext_bridge({"content": [{"type": "text", "text": dirty}], "isError": False})
    res_text = asyncio.run(bridge_text.call("docs", "lookup", {}, context=ctx))
    assert res_text.value == {"text": "okREDevil\n\tkept"}

    bridge_struct = _ext_bridge({
        "structuredContent": {"k\u202e": [dirty, {"nested": "\x1b]0;title\x07clean\x00"}]},
        "isError": False,
    })
    res_struct = asyncio.run(bridge_struct.call("docs", "lookup", {}, context=ctx))
    assert res_struct.value == {"k": ["okREDevil\n\tkept", {"nested": "clean"}]}


# ---------------------------------------------------------------------------
# 5. ExtensionHost (env scrubbing, JSON hostile edge cases, sanitization)
# ---------------------------------------------------------------------------

def test_extension_host_scrubs_parent_and_explicit_secrets(monkeypatch):
    monkeypatch.setenv("SONDER_API_KEY", "super-secret-sonder-key")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_token_value")
    script = (
        "import json, os, sys\n"
        'print(json.dumps({"type":"ready"}), flush=True)\n'
        "for line in sys.stdin:\n"
        "    r = json.loads(line)\n"
        '    print(json.dumps({"id": r["id"], "keys": sorted(os.environ.keys())}), flush=True)\n'
    )
    host = ExtensionHost([sys.executable, "-c", script])
    try:
        res = host.call("env_check")
        assert "SONDER_API_KEY" not in res["keys"]
        assert "GITHUB_TOKEN" not in res["keys"]
    finally:
        host.close()

    explicit_host = ExtensionHost(
        [sys.executable, "-c", script],
        env={"SAFE_CUSTOM": "1", "MY_SECRET_TOKEN": "nope", "SONDER_OPENAI_API_KEY": "sk-no"},
    )
    try:
        res2 = explicit_host.call("env_check")
        assert "SAFE_CUSTOM" in res2["keys"]
        assert "MY_SECRET_TOKEN" not in res2["keys"]
        assert "SONDER_OPENAI_API_KEY" not in res2["keys"]
    finally:
        explicit_host.close()


@pytest.mark.parametrize("payload_expr", [
    '\'{"id":1,"n":\' + ("9"*5000) + "}"',
    '\'{"id":1,"x":\' + ("["*300) + ("]"*300) + "}"',
    '\'{"id":1,"x":NaN}\'',
])
def test_extension_host_rejects_hostile_json_without_hanging(payload_expr):
    script = (
        "import json, sys\n"
        'print(json.dumps({"type":"ready"}), flush=True)\n'
        "for line in sys.stdin:\n"
        f"    print({payload_expr}, flush=True)\n"
    )
    host = ExtensionHost(
        [sys.executable, "-c", script],
        limits=ExtensionHostLimits(call_timeout_seconds=2.0, max_restarts=0),
    )
    try:
        with pytest.raises(ExtensionHostProtocolError):
            host.call("bad")
    finally:
        host.close()


def test_extension_host_sanitizes_untrusted_response_strings():
    script = (
        "import json, sys\n"
        'print(json.dumps({"type":"ready"}), flush=True)\n'
        "for line in sys.stdin:\n"
        "    r = json.loads(line)\n"
        '    print(json.dumps({"id": r["id"], "msg": "hi\\x1b[31m!\\x1b[0m\\x00\\u202eok"}), flush=True)\n'
    )
    host = ExtensionHost([sys.executable, "-c", script])
    try:
        res = host.call("greet")
        assert res == {"id": 1, "msg": "hi!ok"}
    finally:
        host.close()


# ---------------------------------------------------------------------------
# 6. ToolExecutorAdapter (fetch_artifact cloud_allowed consent gate)
# ---------------------------------------------------------------------------

def test_tool_executor_fetch_artifact_requires_cloud_allowed(monkeypatch):
    import sonder_runtime.adapters.artifact_fetch as artifact_fetch

    called = []
    monkeypatch.setattr(
        artifact_fetch, "fetch_artifact",
        lambda **kwargs: called.append(kwargs) or {"ok": True},
    )
    monkeypatch.setattr(artifact_fetch, "format_fetch_result", lambda _res: "fetched")

    assert "fetch_artifact" in _NETWORK_REFUSAL_TOOLS

    denied_ctx = local_owner_context(correlation_id="fa-deny", cloud_allowed=False)
    res_denied = ToolExecutorAdapter().execute(
        ToolCall("fetch_artifact", {"url": "https://example.test/a.bin", "dest": "a.bin"}),
        denied_ctx,
    )
    assert res_denied.ok is False
    assert res_denied.error_code == "PermissionError"
    assert "cloud consent" in res_denied.output
    assert called == []

    allowed_ctx = local_owner_context(correlation_id="fa-allow", cloud_allowed=True)
    res_allowed = ToolExecutorAdapter().execute(
        ToolCall("fetch_artifact", {"url": "https://example.test/a.bin", "dest": "a.bin"}),
        allowed_ctx,
    )
    assert res_allowed.ok is True
    assert len(called) == 1


# ---------------------------------------------------------------------------
# 7. OpenAICompatibleGateway (key repr/CRLF, query/fragment, RecursionError, HTTPError.close)
# ---------------------------------------------------------------------------

def test_openai_config_hides_api_key_in_repr_and_rejects_control_chars():
    secret = "sk-super-secret-bearer-token-123"
    cfg = OpenAICompatibleConfig(base_url="https://api.example.com", api_key=secret, model="m")
    assert secret not in repr(cfg)

    bad_secret = "sk-leak-check\r\nX-Injected: 1"
    with pytest.raises(InvalidInput) as exc_info:
        OpenAICompatibleConfig(base_url="https://api.example.com", api_key=bad_secret, model="m")
    assert "sk-leak-check" not in str(exc_info.value)

    gw = OpenAICompatibleGateway(cfg, spend_ledger=None)
    duck_cfg = SimpleNamespace(api_key=bad_secret)
    with pytest.raises(InvalidInput) as exc_info2:
        gw._headers(duck_cfg)  # type: ignore[arg-type]
    assert "sk-leak-check" not in str(exc_info2.value)


@pytest.mark.parametrize("url", [
    "https://api.example.com/v1?foo=bar",
    "https://api.example.com/v1#section",
    "http://127.0.0.1:8080/v1?api_key=secret",
])
def test_openai_require_secure_transport_rejects_query_and_fragment(url):
    cfg = OpenAICompatibleConfig(base_url=url, model="m")
    gw = OpenAICompatibleGateway(cfg, spend_ledger=None)
    with pytest.raises(InvalidInput) as exc_info:
        gw._require_secure_transport(cfg)
    assert "secret" not in str(exc_info.value)
    assert "query" in str(exc_info.value) or "fragment" in str(exc_info.value)


def test_openai_default_transport_catches_recursion_error(monkeypatch):
    deep = ("[" * 5000 + "]" * 5000).encode("utf-8")

    class _Resp:
        def read(self, n=-1):
            nonlocal deep
            chunk, deep = (deep[:n], deep[n:]) if n >= 0 else (deep, b"")
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(
        oai_mod, "_opener_for",
        lambda _url: SimpleNamespace(open=lambda _req, timeout=None: _Resp()),
    )
    with pytest.raises(DependencyUnavailable, match="non-JSON"):
        OpenAICompatibleGateway._default_transport(
            "http://127.0.0.1:8080/v1/chat/completions", {"messages": []}, {}, 5.0,
        )


def test_openai_closes_http_error_in_post_and_get(monkeypatch):
    closed = []

    class _TrackingHTTPError(urllib.error.HTTPError):
        def close(self):
            closed.append(self.code)
            super().close()

    def _raise_post(*_args, **_kwargs):
        raise _TrackingHTTPError(
            "http://127.0.0.1:8080/v1/chat/completions", 502, "Bad Gateway", {}, io.BytesIO(b"err"),
        )

    cfg = OpenAICompatibleConfig(base_url="http://127.0.0.1:8080", model="m")
    gw = OpenAICompatibleGateway(cfg, transport=_raise_post, spend_ledger=None)
    with pytest.raises(DependencyUnavailable):
        gw._post("/v1/chat/completions", {"messages": []}, cfg, 5.0)
    assert closed == [502]

    def _raise_get(_req, timeout=None):
        raise _TrackingHTTPError(
            "http://127.0.0.1:8080/health", 503, "Unavailable", {}, io.BytesIO(b'{"ok":false}'),
        )

    monkeypatch.setattr(oai_mod, "_opener_for", lambda _url: SimpleNamespace(open=_raise_get))
    status, body = OpenAICompatibleGateway._default_get_transport("http://127.0.0.1:8080/health", {}, 5.0)
    assert status == 503
    assert body == b'{"ok":false}'
    assert closed == [502, 503]


# ---------------------------------------------------------------------------
# 8. Embeddings (bounded HTTP response reads)
# ---------------------------------------------------------------------------

class _ChunkedInfiniteResponse:
    def __init__(self, chunk: bytes):
        self._chunk = chunk
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise AssertionError("unbounded read() must not be called")
        out = (self._chunk * ((size // len(self._chunk)) + 1))[:size]
        self.bytes_read += len(out)
        return out

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_embeddings_bounds_tags_and_embeddings_http_reads(monkeypatch):
    monkeypatch.setattr(embeddings_mod, "_TAGS_RESPONSE_LIMIT", 1024)
    monkeypatch.setattr(embeddings_mod, "_EMBED_RESPONSE_LIMIT", 2048)

    tags_resp = _ChunkedInfiniteResponse(b"x" * 256)
    monkeypatch.setattr(
        embeddings_mod.ollama_endpoint, "open_url", lambda *_a, **_k: tags_resp,
    )
    assert embeddings_mod.serving_model_revision(model="nomic-embed-text", base="http://127.0.0.1:11434") == ""
    assert tags_resp.bytes_read == 1025

    embed_resp = _ChunkedInfiniteResponse(b"y" * 256)
    monkeypatch.setattr(
        embeddings_mod.ollama_endpoint, "open_url", lambda *_a, **_k: embed_resp,
    )
    monkeypatch.setattr(embeddings_mod, "current_revision", lambda **_k: "rev-1")
    monkeypatch.setattr(embeddings_mod.embed_cache, "get", lambda *_a, **_k: None)
    monkeypatch.setattr(embeddings_mod, "_accelerated_embed", lambda *_a, **_k: None)
    assert embeddings_mod._embed_at("hello", base="http://127.0.0.1:11434", model="nomic-embed-text") is None
    assert embed_resp.bytes_read == 2049
    assert getattr(embeddings_mod._EMBED_STATE, "transport_failed", False) is False
