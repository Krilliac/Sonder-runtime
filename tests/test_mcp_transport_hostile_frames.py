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
from sonder_runtime.interfaces.mcp.transport import StdioMcpTransport

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
