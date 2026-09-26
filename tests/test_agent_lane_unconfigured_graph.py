"""The legacy ``agent_lane`` tool on the graph ``server.py`` composes itself.

Bare loopback ``python server.py`` builds its application lazily and without
a typed configuration (``legacy_root.require_mcp_inference_binding`` accepts
exactly that graph). The lane tool used to read ``application.config.state``
from it and crash with ``AttributeError``; it must refuse with a typed error
instead, on the direct call, the MCP call and the MCP gate path.
"""
import asyncio

import pytest

from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.domain.common.errors import DependencyUnavailable


@pytest.fixture
def server_owned_graph(monkeypatch):
    import server

    monkeypatch.setattr(server, "_APP_GRAPH", None)
    monkeypatch.setattr(server, "_APP_GRAPH_OWNED_BY_SERVER", False)
    graph = server._application()
    assert graph.config is None
    try:
        yield server
    finally:
        server._close_server_owned_application(timeout=5)
        bootstrap_app.reset_for_tests()


def _assert_typed_refusal(exc):
    # FastMCP may wrap the tool's exception; the typed cause must survive.
    seen = exc
    while seen is not None and not isinstance(seen, DependencyUnavailable):
        seen = seen.__cause__ or seen.__context__
    assert isinstance(seen, DependencyUnavailable), repr(exc)
    assert "python -m sonder_runtime mcp" in str(seen)
    assert not isinstance(exc, AttributeError)


def test_direct_agent_lane_refuses_without_a_configured_graph(server_owned_graph):
    with pytest.raises(Exception) as raised:
        server_owned_graph.agent_lane("list", {})
    _assert_typed_refusal(raised.value)


def test_mcp_agent_lane_refuses_without_a_configured_graph(server_owned_graph):
    with pytest.raises(Exception) as raised:
        asyncio.run(server_owned_graph.mcp.call_tool(
            "agent_lane", {"action": "list", "payload": {}},
        ))
    _assert_typed_refusal(raised.value)


def test_open_parent_refuses_without_a_configured_graph(server_owned_graph):
    with pytest.raises(Exception) as raised:
        asyncio.run(server_owned_graph.mcp.call_tool(
            "agent_lane", {"action": "open_parent", "payload": {}},
        ))
    _assert_typed_refusal(raised.value)


def test_agent_loop_lane_tool_refuses_without_a_configured_graph(server_owned_graph):
    # The ``agent``/``workbench_agent`` tools open a lane controller on
    # whatever graph ``server._application`` returns; on this graph the
    # model's own ``agent_lane`` call must come back as a host-policy refusal,
    # not an AttributeError escaping the agent loop.
    from sonder_runtime.interfaces import standalone_agent_lanes

    with standalone_agent_lanes.controller_scope(server_owned_graph._application) as controller:
        assert controller is not None and controller.available
        answer = server_owned_graph._agent_dispatch(
            "agent_lane", {"action": "list", "payload": {}},
        )
    assert isinstance(answer, str)
    assert answer.startswith("ERROR: HOST POLICY:"), answer
    assert "configured runtime" in answer
