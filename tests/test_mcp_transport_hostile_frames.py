"""RC7: a small hostile frame must not end the native MCP serve loop.

_decode_frame caught TypeError and JSONDecodeError only, but json.loads also
raises a plain ValueError (an integer over the 4300-digit conversion limit)
and RecursionError (deep nesting), and serve() caught neither, so one short
line killed the session. Each must become a JSON-RPC parse error, with the
next request still answered.
"""
import io
import json

import pytest

from sonder_runtime.application.protocol.mcp_compatibility import McpCompatibility
from sonder_runtime.interfaces.mcp.transport import McpTransportLimits, StdioMcpTransport

PING = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"})

HOSTILE = {
    "5000-digit integer": '{"jsonrpc": "2.0", "id": 1, "n": %s}' % ("9" * 5000),
    "bare 5000-digit integer": "9" * 5000,
    "10k-deep nesting": "[" * 10000 + "]" * 10000,
    "10k-deep object nesting": '{"a":' * 10000 + "1" + "}" * 10000,
    "lone surrogate": '{"jsonrpc": "2.0", "id": 1, "method": "\ud800"}',
}


@pytest.mark.parametrize("frame", list(HOSTILE.values()), ids=list(HOSTILE))
def test_hostile_frame_is_a_parse_error_and_the_session_survives(frame):
    output = io.StringIO()
    transport = StdioMcpTransport(
        io.StringIO(frame + "\n" + PING + "\n"), output,
        compatibility=McpCompatibility(), tool_catalog=(), tool_handler=lambda *_: {},
    )

    assert transport.serve() == 2

    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == -32700
    assert replies[0]["id"] is None
    assert replies[1] == {"jsonrpc": "2.0", "id": 7, "result": {}}


def _serve(lines, **kwargs):
    output = io.StringIO()
    transport = StdioMcpTransport(
        io.StringIO("".join(line + "\n" for line in lines)), output,
        compatibility=McpCompatibility(), tool_catalog=(), tool_handler=lambda *_: {},
        **kwargs,
    )
    handled = transport.serve()
    return handled, [json.loads(line) for line in output.getvalue().splitlines()]


def _ping_with_params(request_id, params_json):
    return '{"jsonrpc": "2.0", "id": %d, "method": "ping", "params": %s}' % (
        request_id, params_json,
    )


def test_frame_nested_past_max_json_depth_is_a_parse_error():
    # 300 levels parse fine under json.loads on every supported Python, so
    # only the depth bound refuses this otherwise valid ping.
    deep = '{"a": %s}' % ("[" * 300 + "]" * 300)
    handled, replies = _serve([_ping_with_params(1, deep), PING])

    assert handled == 2
    assert replies[0]["error"]["code"] == -32700
    assert "max_json_depth" in replies[0]["error"]["message"]
    assert replies[1] == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_brackets_inside_strings_do_not_count_toward_depth():
    text = '{"s": "%s\\"%s"}' % ("[" * 1000, "{" * 1000)
    handled, replies = _serve([_ping_with_params(3, text)])

    assert handled == 1
    assert replies == [{"jsonrpc": "2.0", "id": 3, "result": {}}]


def test_max_json_depth_is_configurable():
    limits = McpTransportLimits(max_json_depth=3)
    # The frame object, params and one list: depth 3 is allowed, 4 is not.
    _, allowed = _serve([_ping_with_params(4, "{\"a\": [1]}")], limits=limits)
    _, refused = _serve([_ping_with_params(5, "{\"a\": [[1]]}")], limits=limits)

    assert allowed == [{"jsonrpc": "2.0", "id": 4, "result": {}}]
    assert refused[0]["error"]["code"] == -32700


@pytest.mark.parametrize("bad", [0, -1, True])
def test_max_json_depth_must_be_a_positive_int(bad):
    with pytest.raises(ValueError):
        McpTransportLimits(max_json_depth=bad)


@pytest.mark.parametrize("bad", [0, -1, True])
def test_max_discard_bytes_must_be_a_positive_int(bad):
    with pytest.raises(ValueError):
        McpTransportLimits(max_discard_bytes=bad)


def test_oversized_frame_exceeding_max_discard_bytes_aborts_stream():
    class _InfiniteNoNewlineStream:
        def __init__(self):
            self.reads = 0

        def readline(self, size: int = -1) -> str:
            self.reads += 1
            return "A" * (size if size and size > 0 else 1024)

    stream = _InfiniteNoNewlineStream()
    output = io.StringIO()
    limits = McpTransportLimits(max_frame_bytes=256, max_discard_bytes=1024)
    transport = StdioMcpTransport(
        stream, output,
        compatibility=McpCompatibility(), tool_catalog=(), tool_handler=lambda *_: {},
        limits=limits,
    )
    handled = transport.serve()
    assert handled == 1
    assert stream.reads <= 10
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(replies) == 1
    assert replies[0]["error"]["code"] == -32700
    assert "max_discard_bytes" in replies[0]["error"]["message"]
