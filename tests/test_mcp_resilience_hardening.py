"""Regression tests for Wave 3 MCP adapter and interface resilience hardening."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters import external_mcp, mcp_subprocess
from sonder_runtime.adapters.external_mcp import (
    ExternalMcpBridge,
    ExternalMcpError,
    ExternalMcpServerPolicy,
    ExternalMcpToolPolicy,
)
from sonder_runtime.adapters.mcp_subprocess import (
    McpProviderCancelled,
    McpProviderOutputLimit,
    McpProviderProtocolError,
    McpProviderTimeout,
    McpSubprocessProvider,
)
from sonder_runtime.application.context import (
    current_operation_context,
    local_owner_context,
)
from sonder_runtime.application.execution.tool_service import ToolService
from sonder_runtime.application.ports.tool_executor import (
    ToolResult as ExecutorToolResult,
)
from sonder_runtime.application.protocol.mcp_compatibility import (
    McpCompatibility,
    SubscriptionNotificationRouter,
)
from sonder_runtime.domain.tools.descriptors import (
    ExecutionClass,
    ToolDescriptor,
    ToolResult as DomainToolResult,
)
from sonder_runtime.domain.tools.traits import TriState
from sonder_runtime.interfaces.mcp.handlers import (
    McpOutcomeHandler,
    McpRecallHandler,
    McpToolHandler,
    context_for_mcp_call,
)
from sonder_runtime.interfaces.mcp.transport import (
    BoundedMcpProviderExchange,
    McpTransportError,
    McpTransportLimits,
    StdioMcpTransport,
)
from sonder_runtime.platform.runtime_threads import (
    Thread as native_owned_thread,
    ThreadOwnershipRefused,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. StdioMcpTransport & BoundedMcpProviderExchange
# ---------------------------------------------------------------------------


def test_whitespace_oversized_prefix_cannot_smuggle_trailing_frame():
    limit = 128
    smuggled = json.dumps({"jsonrpc": "2.0", "id": 66, "method": "ping"})
    valid = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    stream = io.StringIO(" " * (limit + 1) + smuggled + "\n" + valid + "\n")
    output = io.StringIO()
    transport = StdioMcpTransport(
        stream,
        output,
        compatibility=McpCompatibility(),
        tool_catalog=(),
        tool_handler=lambda *_: {},
        limits=McpTransportLimits(max_frame_bytes=limit),
    )

    assert transport.serve() == 2
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [row.get("id") for row in replies] == [None, 1]
    assert replies[0]["error"]["code"] == -32700
    assert "max_frame_bytes" in replies[0]["error"]["message"]
    assert replies[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_infinite_whitespace_stream_aborts_at_max_discard_bytes():
    class _InfiniteSpaces:
        def __init__(self) -> None:
            self.reads = 0

        def readline(self, size: int = -1) -> str:
            self.reads += 1
            return " " * (size if size and size > 0 else 256)

    stream = _InfiniteSpaces()
    output = io.StringIO()
    transport = StdioMcpTransport(
        stream,
        output,
        compatibility=McpCompatibility(),
        tool_catalog=(),
        tool_handler=lambda *_: {},
        limits=McpTransportLimits(max_frame_bytes=128, max_discard_bytes=512),
    )

    assert transport.serve() == 1
    assert stream.reads <= 8
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(replies) == 1
    assert replies[0]["error"]["code"] == -32700
    assert "max_discard_bytes" in replies[0]["error"]["message"]


def test_text_stream_unicode_decode_error_emits_parse_error_and_unsubscribes():
    # Real TextIOWrapper on invalid UTF-8 bytes fails inside readline().
    raw_out = io.StringIO()
    raw_transport = StdioMcpTransport(
        io.TextIOWrapper(io.BytesIO(b"\xff\xfe\n"), encoding="utf-8"),
        raw_out,
        compatibility=McpCompatibility(),
        tool_catalog=(),
        tool_handler=lambda *_: {},
    )
    assert raw_transport.serve() == 1
    raw_reply = json.loads(raw_out.getvalue())
    assert raw_reply["error"]["code"] == -32700
    assert "UTF-8" in raw_reply["error"]["message"]

    # Mid-session UnicodeDecodeError after subscribing must still unsubscribe.
    class _DecodeFailOnThirdLine:
        def __init__(self, lines: list[str]) -> None:
            self._lines = list(lines)

        def readline(self, _size: int = -1) -> str:
            if self._lines:
                return self._lines.pop(0)
            raise UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte")

    router = SubscriptionNotificationRouter()
    init = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2.0", "capabilities": {"notifications": {}}},
    }) + "\n"
    sub = json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "sonder/subscribe",
        "params": {"event": "job.updated"},
    }) + "\n"
    output = io.StringIO()
    transport = StdioMcpTransport(
        _DecodeFailOnThirdLine([init, sub]),  # type: ignore[arg-type]
        output,
        compatibility=McpCompatibility(capabilities=("notifications",)),
        tool_catalog=(),
        tool_handler=lambda *_: {},
        notifications=router,
        connection_id="bad-utf8",
    )

    assert transport.serve() == 3
    assert router.subscriber_count() == 0
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[2]["error"]["code"] == -32700
    assert "UTF-8" in replies[2]["error"]["message"]


def test_abrupt_eof_partial_frame_returns_parse_error_and_unsubscribes():
    router = SubscriptionNotificationRouter()
    lines = "\n".join([
        json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2.0"},
        }),
        json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "sonder/subscribe",
            "params": {"event": "tick"},
        }),
        '{"jsonrpc":"2.0","id":3,"method":"ping"',  # truncated, no newline
    ])
    output = io.StringIO()
    transport = StdioMcpTransport(
        io.StringIO(lines),
        output,
        compatibility=McpCompatibility(),
        tool_catalog=(),
        tool_handler=lambda *_: {},
        notifications=router,
        connection_id="partial-eof",
    )

    assert transport.serve() == 3
    assert router.subscriber_count() == 0
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[2]["error"]["code"] == -32700


def test_broken_pipe_on_output_exits_serve_cleanly_and_unsubscribes():
    class _BrokenOutput:
        def __init__(self) -> None:
            self.writes = 0

        def write(self, data: str) -> int:
            self.writes += 1
            if self.writes >= 3:
                raise BrokenPipeError("peer disconnected")
            return len(data)

        def flush(self) -> None:
            pass

    router = SubscriptionNotificationRouter()
    frames = "\n".join(json.dumps(item) for item in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2.0"}},
        {"jsonrpc": "2.0", "id": 2, "method": "sonder/subscribe", "params": {"event": "tick"}},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "method": "ping"},
    ]) + "\n"
    transport = StdioMcpTransport(
        io.StringIO(frames),
        _BrokenOutput(),  # type: ignore[arg-type]
        compatibility=McpCompatibility(),
        tool_catalog=(),
        tool_handler=lambda *_: {},
        notifications=router,
        connection_id="broken-pipe",
    )

    assert transport.serve() == 3
    assert router.subscriber_count() == 0


def test_tiny_max_frame_bytes_fallback_error_never_crashes_serve():
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}, separators=(",", ":"))
    assert len(ping) <= 44
    output = io.StringIO()
    transport = StdioMcpTransport(
        io.StringIO(ping + "\n" + "X" * 100 + "\n" + ping + "\n"),
        output,
        compatibility=McpCompatibility(),
        tool_catalog=(),
        tool_handler=lambda *_: {},
        limits=McpTransportLimits(max_frame_bytes=44),
    )

    assert transport.serve() == 3
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert replies[-1] == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_jsonrpc_notifications_never_emit_error_responses():
    frames = "\n".join(json.dumps(item) for item in [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2.0"}},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 99}},
        {"jsonrpc": "2.0", "method": "unknown/notification", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]) + "\n"
    output = io.StringIO()
    transport = StdioMcpTransport(
        io.StringIO(frames),
        output,
        compatibility=McpCompatibility(),
        tool_catalog=(),
        tool_handler=lambda *_: {},
    )

    assert transport.serve() == 5
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [row["id"] for row in replies] == [1, 2]
    assert replies[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_internal_keyerror_in_tool_handler_does_not_leak_secret_key():
    def handler(name: str, _args: dict):
        if name == "secret_bug":
            raise KeyError("sk-live-secret-key-must-not-leak")
        raise KeyError(name)

    frames = "\n".join(json.dumps(item) for item in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2.0"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "secret_bug", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "missing_tool", "arguments": {}}},
    ]) + "\n"
    output = io.StringIO()
    transport = StdioMcpTransport(
        io.StringIO(frames),
        output,
        compatibility=McpCompatibility(),
        tool_catalog=(),
        tool_handler=handler,
    )

    assert transport.serve() == 3
    raw_output = output.getvalue()
    assert "sk-live-secret-key-must-not-leak" not in raw_output
    replies = [json.loads(line) for line in raw_output.splitlines()]
    assert replies[1]["error"] == {"code": -32603, "message": "internal MCP handler error"}
    assert replies[2]["error"]["code"] == -32601


def test_notification_fault_isolation_and_post_close_suppression():
    class _FailingOutput:
        def write(self, _data: str) -> int:
            raise BrokenPipeError("dead peer")

    router = SubscriptionNotificationRouter()
    healthy_out = io.StringIO()
    broken = StdioMcpTransport(
        io.StringIO(""),
        _FailingOutput(),  # type: ignore[arg-type]
        compatibility=McpCompatibility(),
        tool_catalog=(),
        tool_handler=lambda *_: {},
        notifications=router,
        connection_id="broken",
    )
    healthy = StdioMcpTransport(
        io.StringIO(""),
        healthy_out,
        compatibility=McpCompatibility(),
        tool_catalog=(),
        tool_handler=lambda *_: {},
        notifications=router,
        connection_id="healthy",
        limits=McpTransportLimits(max_frame_bytes=256),
    )
    router.subscribe("broken", "ev", broken._on_notification)
    router.subscribe("healthy", "ev", healthy._on_notification)

    # First publish hits BrokenPipeError on `broken`, which auto-closes and
    # unsubscribes itself without interrupting delivery to `healthy`.
    router.publish("ev", {"ok": 1})
    assert router.subscriber_count("ev") == 1
    assert [json.loads(line)["params"]["payload"] for line in healthy_out.getvalue().splitlines()] == [{"ok": 1}]

    # Oversized and non-finite notifications are dropped safely without crashing publish().
    router.publish("ev", {"huge": "x" * 500})
    router.publish("ev", {"bad": float("nan")})
    router.publish("ev", {"ok": 2})
    assert [json.loads(line)["params"]["payload"] for line in healthy_out.getvalue().splitlines()] == [
        {"ok": 1}, {"ok": 2},
    ]

    # After close(), notifications do not write to the stream.
    healthy.close()
    healthy._on_notification("ev", {"after_close": True})
    assert len(healthy_out.getvalue().splitlines()) == 2


def test_catalog_generator_fails_fast_at_max_tools():
    consumed = 0

    def _gen():
        nonlocal consumed
        for i in range(100_000):
            consumed += 1
            yield {"name": f"t{i}"}

    with pytest.raises(McpTransportError, match="max_tools"):
        StdioMcpTransport(
            io.StringIO(""),
            io.StringIO(),
            compatibility=McpCompatibility(),
            tool_catalog=_gen(),
            tool_handler=lambda *_: {},
            limits=McpTransportLimits(max_tools=3),
        )
    assert consumed == 4


def test_bounded_provider_exchange_bounds_stderr_surrogates_and_forwards_limits():
    class _StderrProvider:
        def run(self, _request: str) -> tuple[str, str]:
            return "ok", "e" * 32

    exchange = BoundedMcpProviderExchange(
        _StderrProvider(),
        limits=McpTransportLimits(max_exchange_bytes=16),
    )
    with pytest.raises(McpTransportError, match="stderr exceeds"):
        exchange.exchange("hi")
    with pytest.raises(McpTransportError, match="UTF-8"):
        exchange.exchange("\ud800")

    seen_kwargs: dict[str, object] = {}

    class _KwargProvider:
        def run(
            self,
            request: str,
            *,
            cancel_check=None,
            deadline_monotonic=None,
            max_output_bytes=None,
            max_stderr_bytes=None,
        ) -> tuple[str, str]:
            seen_kwargs.update({
                "cancel_check": cancel_check,
                "deadline_monotonic": deadline_monotonic,
                "max_output_bytes": max_output_bytes,
                "max_stderr_bytes": max_stderr_bytes,
            })
            return request, ""

    cancel = lambda: False  # noqa: E731
    deadline = time.monotonic() + 5.0
    ex2 = BoundedMcpProviderExchange(
        _KwargProvider(),
        limits=McpTransportLimits(max_exchange_bytes=64),
    )
    assert ex2.exchange("ok", cancel_check=cancel, deadline_monotonic=deadline) == ("ok", "")
    assert seen_kwargs["max_output_bytes"] == 64
    assert seen_kwargs["max_stderr_bytes"] == 64
    assert seen_kwargs["cancel_check"] is cancel
    assert seen_kwargs["deadline_monotonic"] == deadline

    # Output-limit error from subprocess provider maps cleanly to McpTransportError.
    class _OverflowProvider:
        def run(self, _request: str, **_kwargs) -> tuple[str, str]:
            raise McpProviderOutputLimit("too big")

    ex3 = BoundedMcpProviderExchange(_OverflowProvider(), limits=McpTransportLimits(max_exchange_bytes=64))
    with pytest.raises(McpTransportError, match="max_exchange_bytes"):
        ex3.exchange("ok")


def test_transport_limits_reject_float_values():
    with pytest.raises(ValueError, match="positive integers"):
        McpTransportLimits(max_frame_bytes=128.5)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. Thin MCP handlers (handlers.py)
# ---------------------------------------------------------------------------


def test_mcp_tool_handler_works_with_real_tool_service_domain_and_port_results():
    class _Registry:
        def __init__(self) -> None:
            self._tools = {
                "echo": ToolDescriptor(
                    name="echo",
                    input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
                    execution_class=ExecutionClass.PURE,
                ),
            }

        def get(self, name: str):
            return self._tools.get(name)

        def list_all(self):
            return list(self._tools.values())

    class _Executor:
        def __init__(self, *, success: bool) -> None:
            self._success = success

        def execute(self, descriptor, call, context, execution_class):
            assert current_operation_context() is context
            assert context.source == "mcp"
            return DomainToolResult(
                tool_name=call.tool_name,
                output="done" if self._success else "",
                error="" if self._success else "tool failed",
                success=self._success,
            )

    class _AllowPolicy:
        def authorize(self, _descriptor, _call) -> None:
            return None

        def select_execution_class(self, descriptor):
            return descriptor.execution_class

    ok_handler = McpToolHandler(ToolService(_Registry(), _AllowPolicy(), _Executor(success=True)))
    ok_res = ok_handler.handle("echo", {"x": "hi"})
    assert ok_res == {"output": "done", "isError": False}

    fail_handler = McpToolHandler(ToolService(_Registry(), _AllowPolicy(), _Executor(success=False)))
    fail_res = fail_handler.handle("echo", {"x": "hi"})
    assert fail_res == {"output": "", "isError": True, "error": "tool failed"}

    # Also supports ports.tool_executor.ToolResult (`.ok` attribute).
    port_handler = McpToolHandler(SimpleNamespace(
        execute=lambda _call, _ctx: ExecutorToolResult(ok=False, output="denied", error_code="FORBIDDEN"),
    ))
    assert port_handler.handle("echo", {}) == {
        "output": "denied", "isError": True, "error": "FORBIDDEN",
    }


def test_mcp_handlers_validate_inputs_bind_context_and_hide_internal_errors():
    observed_sources: list[str] = []

    class _RecallSvc:
        def recall(self, task: str, *, k: int = 2, project=None):
            ctx = current_operation_context()
            assert ctx is not None
            observed_sources.append(ctx.source)
            if task == "explode":
                raise RuntimeError("sqlite error with token=sk-super-secret-123")
            return [f"{task}:{k}"]

    class _OutcomeSvc:
        def record(self, interaction_id: str, signal: str):
            ctx = current_operation_context()
            assert ctx is not None
            observed_sources.append(ctx.source)
            if interaction_id == "explode":
                raise RuntimeError("db error Bearer secret-xyz")
            return 1.0

    recall = McpRecallHandler(_RecallSvc())
    assert recall.handle({"task": "ok", "k": 1}) == {"results": ["ok:1"]}
    assert recall.handle("not-a-dict")["error"] == "INVALID_INPUT"  # type: ignore[arg-type]
    assert recall.handle({"task": 123})["error"] == "INVALID_INPUT"
    assert recall.handle({"task": "ok", "k": True})["error"] == "INVALID_INPUT"
    assert recall.handle({"task": "ok", "k": 0})["error"] == "INVALID_INPUT"
    assert recall.handle({"task": "ok", "project": 42})["error"] == "INVALID_INPUT"
    recall_err = recall.handle({"task": "explode"})
    assert recall_err == {
        "isError": True,
        "error": "INTERNAL_FAILURE",
        "message": "internal recall failure",
        "retryable": False,
    }
    assert "sk-super-secret-123" not in json.dumps(recall_err)

    outcome = McpOutcomeHandler(_OutcomeSvc())
    assert outcome.handle({"interaction_id": "id1", "signal": "good"}) == {"score": 1.0}
    assert outcome.handle(None)["error"] == "INVALID_INPUT"  # type: ignore[arg-type]
    assert outcome.handle({"interaction_id": "   ", "signal": "good"})["error"] == "INVALID_INPUT"
    assert outcome.handle({"interaction_id": 123, "signal": "good"})["error"] == "INVALID_INPUT"
    outcome_err = outcome.handle({"interaction_id": "explode", "signal": "good"})
    assert outcome_err["error"] == "INTERNAL_FAILURE"
    assert "secret-xyz" not in json.dumps(outcome_err)

    tools = McpToolHandler(SimpleNamespace(
        execute=lambda _c, _ctx: (_ for _ in ()).throw(RuntimeError("crash token=topsecret")),
    ))
    assert tools.handle("", {})["error"] == "INVALID_INPUT"
    assert tools.handle("echo", ["not-dict"])["error"] == "INVALID_INPUT"  # type: ignore[arg-type]
    tool_err = tools.handle("echo", {})
    assert tool_err["error"] == "INTERNAL_FAILURE"
    assert "topsecret" not in json.dumps(tool_err)

    assert observed_sources == ["mcp", "mcp", "mcp", "mcp"]


@pytest.mark.parametrize("bad_timeout", [0, -1, True, float("nan"), float("inf"), "5"])
def test_context_for_mcp_call_rejects_invalid_timeouts(bad_timeout):
    with pytest.raises(ValueError, match="timeout_seconds"):
        context_for_mcp_call(timeout_seconds=bad_timeout)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. McpSubprocessProvider resilience (adapters/mcp_subprocess.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1.0, True, float("nan"), float("inf")])
def test_subprocess_provider_rejects_non_finite_and_bool_timeouts(bad):
    with pytest.raises(ValueError):
        McpSubprocessProvider(["python"], timeout_seconds=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        McpSubprocessProvider(["python"], shutdown_timeout_seconds=bad)  # type: ignore[arg-type]


def test_subprocess_provider_preserves_utf8_unicode_across_pipes():
    script = (
        "import sys\n"
        "data = sys.stdin.buffer.read().decode('utf-8')\n"
        "sys.stdout.buffer.write(data.encode('utf-8'))\n"
        "sys.stdout.buffer.flush()\n"
    )
    provider = McpSubprocessProvider(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        timeout_seconds=5.0,
    )
    payload = '{"jsonrpc":"2.0","id":1,"text":"雪-café-→-🚀"}\n'
    stdout, stderr = provider.run(payload)
    assert stderr == ""
    assert stdout == payload


def test_subprocess_provider_drains_large_stderr_without_deadlock_and_bounds_retention():
    script = (
        "import sys\n"
        "sys.stderr.buffer.write(b'E' * 262144)\n"
        "sys.stderr.buffer.flush()\n"
        "sys.stdout.buffer.write(b'{\"ok\":true}\\n')\n"
        "sys.stdout.buffer.flush()\n"
    )
    provider = McpSubprocessProvider(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        timeout_seconds=5.0,
        max_stderr_bytes=1024,
        max_stderr_discard_bytes=1024 * 1024,
    )
    stdout, stderr = provider.run("")
    assert json.loads(stdout) == {"ok": True}
    assert len(stderr.encode("utf-8")) == 1024


def test_subprocess_provider_terminates_child_on_oversized_stdout():
    script = (
        "import sys\n"
        "chunk = b'A' * 4096\n"
        "for _ in range(2000):\n"
        "    try:\n"
        "        sys.stdout.buffer.write(chunk)\n"
        "        sys.stdout.buffer.flush()\n"
        "    except BrokenPipeError:\n"
        "        break\n"
    )
    provider = McpSubprocessProvider(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        timeout_seconds=30.0,
        max_output_bytes=8192,
    )
    started = time.monotonic()
    with pytest.raises(McpProviderOutputLimit, match="max_output_bytes"):
        provider.run("")
    assert time.monotonic() - started < 15.0
    assert provider._process is None


def test_subprocess_provider_rejects_invalid_utf8_stdout_cleanly():
    script = (
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xff\\xfe\\n')\n"
        "sys.stdout.buffer.flush()\n"
    )
    provider = McpSubprocessProvider(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        timeout_seconds=5.0,
    )
    with pytest.raises(McpProviderProtocolError, match="UTF-8"):
        provider.run("")
    assert provider._process is None


def test_subprocess_provider_aborts_if_managed_worker_skips_body(monkeypatch):
    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def _recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    class _SkippedThread:
        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return False

        def join(self, _timeout=None) -> None:
            pass

    calls = {"n": 0}

    def _skip_first(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _SkippedThread()
        return native_owned_thread(*args, **kwargs)

    monkeypatch.setattr(mcp_subprocess, "owned_runtime_thread", _skip_first)
    provider = McpSubprocessProvider(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=REPO_ROOT,
        timeout_seconds=10.0,
        popen=_recording_popen,
    )
    with pytest.raises(RuntimeError, match="terminated before running"):
        provider.run("")
    assert len(spawned) == 1
    assert spawned[0].poll() is not None
    assert provider._process is None


def test_subprocess_provider_handles_abrupt_eof_while_writing_large_request():
    script = (
        "import sys\n"
        "sys.stdout.buffer.write(b'{\"early\":true}\\n')\n"
        "sys.stdout.buffer.flush()\n"
    )
    provider = McpSubprocessProvider(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        timeout_seconds=5.0,
    )
    stdout, stderr = provider.run("X" * 262_144)
    assert json.loads(stdout) == {"early": True}
    assert stderr == ""
    assert provider._process is None


def test_subprocess_provider_scrubs_parent_secrets_by_default_but_keeps_explicit_env(monkeypatch):
    monkeypatch.setenv("SONDER_API_KEY", "sonder-secret-value")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_value")
    script = (
        "import json, os, sys\n"
        "keys = sorted(k for k in ('SONDER_API_KEY', 'GITHUB_TOKEN', 'CUSTOM_TOKEN') if k in os.environ)\n"
        "sys.stdout.write(json.dumps(keys) + '\\n')\n"
    )
    default_provider = McpSubprocessProvider([sys.executable, "-c", script], cwd=REPO_ROOT, timeout_seconds=5.0)
    out_default, _ = default_provider.run("")
    assert json.loads(out_default) == []

    explicit_env = {**dict(subprocess.os.environ), "CUSTOM_TOKEN": "explicit-cred"}
    explicit_provider = McpSubprocessProvider(
        [sys.executable, "-c", script], cwd=REPO_ROOT, env=explicit_env, timeout_seconds=5.0,
    )
    out_explicit, _ = explicit_provider.run("")
    assert "CUSTOM_TOKEN" in json.loads(out_explicit)


def test_subprocess_provider_rejects_concurrent_run_and_close_unblocks_active_run():
    started = threading.Event()

    def _observer(event):
        if event.state == "started":
            started.set()

    provider = McpSubprocessProvider(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=REPO_ROOT,
        timeout_seconds=10.0,
        observer=_observer,
    )
    outcome: list[BaseException] = []

    def _runner():
        try:
            provider.run("")
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=_runner)
    thread.start()
    assert started.wait(timeout=5.0)

    with pytest.raises(RuntimeError, match="already active"):
        provider.run("")

    provider.close()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], McpProviderCancelled)
    assert provider._process is None


def test_subprocess_provider_pre_cancelled_or_expired_never_spawns_child():
    spawned = []

    def _fail_popen(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("popen must not run when already cancelled or expired")

    provider = McpSubprocessProvider(["unused"], popen=_fail_popen)
    with pytest.raises(McpProviderCancelled):
        provider.run("", cancel_check=lambda: True)
    with pytest.raises(McpProviderTimeout):
        provider.run("", deadline_monotonic=time.monotonic() - 1.0)
    assert spawned == []


@pytest.mark.parametrize("allowed", [0, 1, 2])
def test_subprocess_provider_thread_refusal_reaps_child_at_each_worker(monkeypatch, allowed):
    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def _recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    calls = {"n": 0}

    def _refuse_after(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > allowed:
            raise ThreadOwnershipRefused("runtime thread capacity exhausted")
        return native_owned_thread(*args, **kwargs)

    monkeypatch.setattr(mcp_subprocess, "owned_runtime_thread", _refuse_after)
    provider = McpSubprocessProvider(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=REPO_ROOT,
        popen=_recording_popen,
    )
    with pytest.raises(ThreadOwnershipRefused):
        provider.run("{}\n")
    assert len(spawned) == 1
    assert spawned[0].poll() is not None
    assert provider._process is None


# ---------------------------------------------------------------------------
# 4. ExternalMcpBridge resilience (adapters/external_mcp.py)
# ---------------------------------------------------------------------------


class _Events:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_code: str, **fields) -> None:
        self.events.append((event_code, fields))


def _ext_server(**changes) -> ExternalMcpServerPolicy:
    values = {
        "name": "docs",
        "endpoint": "http://127.0.0.1:8765/mcp",
        "tools": (ExternalMcpToolPolicy("lookup"),),
    }
    values.update(changes)
    return ExternalMcpServerPolicy(**values)


def _ctx(*, cloud_allowed: bool = False, timeout_seconds: float = 5.0, cancellation=None):
    return local_owner_context(
        correlation_id="resilience-test",
        source="mcp",
        cloud_allowed=cloud_allowed,
        timeout_seconds=timeout_seconds,
        cancellation=cancellation,
    )


def test_external_mcp_gate_serializes_across_multiple_event_loops_and_threads():
    lock = threading.Lock()
    state = {"active": 0, "peak": 0, "calls": 0}

    class _OverlapTransport:
        async def invoke(self, _request):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                state["calls"] += 1
            await asyncio.sleep(0.02)
            with lock:
                state["active"] -= 1
            return {"structuredContent": {"ok": True}}

    bridge = ExternalMcpBridge(
        (_ext_server(),),
        transport=_OverlapTransport(),
        events=_Events(),
        secret_resolver=lambda _n: None,
    )

    async def _pair():
        return await asyncio.gather(
            bridge.call("docs", "lookup", {}, context=_ctx()),
            bridge.call("docs", "lookup", {}, context=_ctx()),
        )

    # Two distinct sequential event loops with contention in each.
    asyncio.run(_pair())
    asyncio.run(_pair())

    # Two OS threads running separate event loops concurrently against the same bridge.
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(asyncio.run, bridge.call("docs", "lookup", {}, context=_ctx()))
        f2 = pool.submit(asyncio.run, bridge.call("docs", "lookup", {}, context=_ctx()))
        assert f1.result().value == {"ok": True}
        assert f2.result().value == {"ok": True}

    assert state["calls"] == 6
    assert state["peak"] == 1


def test_external_mcp_outer_task_cancellation_cancels_transport_and_releases_gate():
    events = _Events()
    started = asyncio.Event()
    cancelled_in_transport = {"value": False}
    call_count = {"n": 0}

    class _Transport:
        async def invoke(self, _request):
            call_count["n"] += 1
            if call_count["n"] == 1:
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled_in_transport["value"] = True
                    raise
            return {"structuredContent": {"recovered": True}}

    bridge = ExternalMcpBridge(
        (_ext_server(),),
        transport=_Transport(),
        events=events,
        secret_resolver=lambda _n: None,
    )

    async def _exercise():
        t1 = asyncio.create_task(bridge.call("docs", "lookup", {}, context=_ctx()))
        await asyncio.wait_for(started.wait(), timeout=2.0)
        t1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t1
        # Gate must be released immediately so a follow-up call succeeds.
        res = await asyncio.wait_for(bridge.call("docs", "lookup", {}, context=_ctx()), timeout=2.0)
        return res

    result = asyncio.run(_exercise())
    assert cancelled_in_transport["value"] is True
    assert result.value == {"recovered": True}
    assert events.events[0][1]["detail"]["error_code"] == "CANCELLED"
    assert events.events[1][1]["detail"]["ok"] is True


def test_external_mcp_gate_waiter_cancellation_does_not_deadlock_next_waiter():
    gate_opened = asyncio.Event()
    release_first = asyncio.Event()

    class _CoordinatedTransport:
        def __init__(self) -> None:
            self.n = 0

        async def invoke(self, _request):
            self.n += 1
            if self.n == 1:
                gate_opened.set()
                await release_first.wait()
            return {"structuredContent": {"n": self.n}}

    bridge = ExternalMcpBridge(
        (_ext_server(),),
        transport=_CoordinatedTransport(),
        events=_Events(),
        secret_resolver=lambda _n: None,
    )

    async def _exercise():
        t1 = asyncio.create_task(bridge.call("docs", "lookup", {}, context=_ctx()))
        await asyncio.wait_for(gate_opened.wait(), timeout=2.0)
        t2 = asyncio.create_task(bridge.call("docs", "lookup", {}, context=_ctx()))
        t3 = asyncio.create_task(bridge.call("docs", "lookup", {}, context=_ctx()))
        await asyncio.sleep(0.02)
        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t2
        release_first.set()
        r1, r3 = await asyncio.wait_for(asyncio.gather(t1, t3), timeout=2.0)
        return r1, r3

    r1, r3 = asyncio.run(_exercise())
    assert r1.value == {"n": 1}
    assert r3.value == {"n": 2}


def test_external_mcp_request_timeout_reflects_gate_wait_time():
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    observed_timeouts: list[float] = []

    class _TimingTransport:
        async def invoke(self, request):
            observed_timeouts.append(request.timeout_seconds)
            if len(observed_timeouts) == 1:
                first_entered.set()
                await release_first.wait()
            return {"structuredContent": {"ok": True}}

    bridge = ExternalMcpBridge(
        (_ext_server(timeout_seconds=2.0),),
        transport=_TimingTransport(),
        events=_Events(),
        secret_resolver=lambda _n: None,
    )

    async def _exercise():
        t1 = asyncio.create_task(bridge.call("docs", "lookup", {}, context=_ctx(timeout_seconds=2.0)))
        await asyncio.wait_for(first_entered.wait(), timeout=2.0)
        t2 = asyncio.create_task(bridge.call("docs", "lookup", {}, context=_ctx(timeout_seconds=1.5)))
        t3 = asyncio.create_task(bridge.call("docs", "lookup", {}, context=_ctx(timeout_seconds=0.05)))
        with pytest.raises(ExternalMcpError) as expired:
            await t3
        await asyncio.sleep(0.05)
        release_first.set()
        await t1
        await t2
        return expired.value.code

    code = asyncio.run(_exercise())
    assert code == "TIMEOUT"
    # Only calls 1 and 2 ran; call 3 expired while waiting on the gate.
    assert len(observed_timeouts) == 2
    assert observed_timeouts[1] < 1.42


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://[64:ff9b::169.254.169.254]/mcp",
        "https://[64:ff9b::127.0.0.1]/mcp",
        "https://[64:ff9b::10.0.0.1]/mcp",
        "https://[64:ff9b:1::10.0.0.1]/mcp",
        "https://[2002:7f00:1::1]/mcp",
    ],
)
def test_external_mcp_blocks_nat64_and_transition_ipv6_ssrf(endpoint):
    bridge = ExternalMcpBridge(
        (_ext_server(endpoint=endpoint, allow_remote=True),),
        transport=SimpleNamespace(invoke=lambda _req: (_ for _ in ()).throw(AssertionError("must block"))),
        events=_Events(),
        secret_resolver=lambda _n: None,
    )
    with pytest.raises(ExternalMcpError) as exc_info:
        asyncio.run(bridge.call("docs", "lookup", {}, context=_ctx(cloud_allowed=True)))
    assert exc_info.value.code == "ENDPOINT_BLOCKED"


@pytest.mark.parametrize("bad_result", [None, "plain-string", 42, [1, 2], object()])
def test_external_mcp_rejects_none_and_primitive_results(bad_result):
    class _Static:
        async def invoke(self, _req):
            return bad_result

    bridge = ExternalMcpBridge(
        (_ext_server(),),
        transport=_Static(),
        events=_Events(),
        secret_resolver=lambda _n: None,
    )
    with pytest.raises(ExternalMcpError) as exc_info:
        asyncio.run(bridge.call("docs", "lookup", {}, context=_ctx()))
    assert exc_info.value.code == "INVALID_RESULT"


def test_external_mcp_honors_snake_case_is_error_when_camel_case_is_none():
    class _Static:
        async def invoke(self, _req):
            return {
                "isError": None,
                "is_error": True,
                "content": [{"type": "text", "text": "upstream failed"}],
            }

    bridge = ExternalMcpBridge(
        (_ext_server(),),
        transport=_Static(),
        events=_Events(),
        secret_resolver=lambda _n: None,
    )
    with pytest.raises(ExternalMcpError) as exc_info:
        asyncio.run(bridge.call("docs", "lookup", {}, context=_ctx()))
    assert exc_info.value.code == "UPSTREAM_TOOL_ERROR"


def test_external_mcp_ingest_tool_list_is_atomic_on_row_limit_error():
    bridge = ExternalMcpBridge(
        (_ext_server(tools=(ExternalMcpToolPolicy("lookup", read_only=True),)),),
        transport=SimpleNamespace(invoke=lambda _r: None),
        events=_Events(),
        secret_resolver=lambda _n: None,
    )
    before = bridge.tool_traits("docs", "lookup")
    assert before is not None and before.read_only is TriState.TRUE

    rows = [{"name": "lookup", "annotations": {"readOnlyHint": False}}] + [
        {"name": f"extra-{i}"} for i in range(external_mcp._MAX_TOOL_LIST_ROWS)
    ]
    with pytest.raises(ExternalMcpError) as exc_info:
        bridge.ingest_tool_list("docs", {"tools": rows})
    assert exc_info.value.code == "INVALID_METADATA"

    after = bridge.tool_traits("docs", "lookup")
    assert after == before
    assert bridge.tool_annotation_traits("docs", "lookup") is None


def test_external_mcp_empty_credential_warning_does_not_log_env_var_name(caplog):
    bridge = ExternalMcpBridge(
        (_ext_server(credential_env="SONDER_TOP_SECRET_CRED_ENV"),),
        transport=SimpleNamespace(invoke=lambda _r: None),
        events=_Events(),
        secret_resolver=lambda _n: "",
    )
    with caplog.at_level(logging.WARNING, logger="sonder_runtime.adapters.external_mcp"):
        with pytest.raises(ExternalMcpError) as exc_info:
            asyncio.run(bridge.call("docs", "lookup", {}, context=_ctx()))
    assert exc_info.value.code == "CREDENTIAL_UNAVAILABLE"
    assert "SONDER_TOP_SECRET_CRED_ENV" not in caplog.text
