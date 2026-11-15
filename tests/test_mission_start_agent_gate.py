"""An agent model must never reach ``mission_start`` (it can launch autopilot)."""
from __future__ import annotations

import pytest

from sonder_runtime.bootstrap import generic_agent_dispatch as dispatch


@pytest.fixture
def launches(monkeypatch):
    import server

    calls = []
    monkeypatch.setattr(server, "_launch_autopilot", lambda *a, **k: calls.append((a, k)) or True)
    monkeypatch.setattr(server, "_require_managed_agent_admission", lambda *a, **k: None)
    return calls


@pytest.mark.parametrize("auto", [True, False])
def test_agent_dispatch_refuses_mission_start(launches, auto):
    import server

    out = server._agent_dispatch("mission_start", {"objective": "x", "auto": auto})
    assert out.startswith("ERROR: HOST POLICY"), out
    assert "system operation" in out
    assert launches == []


def test_mission_start_is_not_agent_reachable_or_advertised():
    import server

    assert "mission_start" in server._AGENT_SYSTEM_OPERATOR_TOOLS
    assert "mission_start" not in dispatch.reachable_names(server.mcp, server._AGENT_SYSTEM_OPERATOR_TOOLS)
    advertised = server._agent_help_advertised_tools(server._agent_tool_help(allow_location=True))
    assert "mission_start" not in advertised
