"""Registry fallback: real SDK schemas, host gates and complete inventory."""
from __future__ import annotations

from typing import Literal
import pytest
from mcp.server.mcpserver import MCPServer

from sonder_runtime.bootstrap import generic_agent_dispatch as dispatch


def _registry(*functions):
    registry = MCPServer("agent routing tests")
    for fn in functions:
        registry.tool()(fn)
    return registry


def _call(name, arguments, registry, **kwargs):
    return dispatch.dispatch(name, arguments, registry, (), run_refusal=lambda *a, **k: "", **kwargs)


def test_registered_alias_uses_registry_callable():
    calls = []

    def sample(value: str = "ok"):
        calls.append(value)
        return "done"

    registry = _registry()
    registry.tool(name="visible_alias")(sample)
    assert _call("visible_alias", {"value": "x"}, registry) == "done"
    assert calls == ["x"]


@pytest.mark.parametrize("name", ["agent", "sonder", "loop", "workbench_agent", "admin_status", "elevate"])
def test_recursive_and_admin_names_are_refused(name):
    calls = []
    registry = _registry()
    registry.tool(name=name)(lambda: calls.append(True))
    assert _call(name, {}, registry).startswith("ERROR: HOST POLICY")
    assert calls == []


@pytest.mark.parametrize("arguments", [
    {}, {"required": "x", "extra": 1}, {"required": 42},
    {"required": "x", "count": "2"}, {"required": "x", "count": True},
    {"required": "x", "mode": "delete"}, {"required": "x", "items": ["bad"]},
])
def test_schema_invalid_arguments_rejected_before_invocation(arguments):
    calls = []

    def sample(required: str, count: int = 1, mode: Literal["read", "write"] = "read", items: list[int] | None = None):
        calls.append(required)

    assert _call("sample", arguments, _registry(sample)).startswith("ERROR: invalid arguments")
    assert calls == []


def test_missing_schema_fails_closed():
    def sample():
        return "must not run"
    registry = _registry(sample)
    registry._tool_manager.get_tool("sample").fn_metadata = None
    assert "schema is unavailable" in _call("sample", {}, registry)


def test_async_registered_tool_is_reachable():
    async def sample(value: int):
        return str(value)
    assert _call("sample", {"value": 3}, _registry(sample)) == "3"


def test_per_run_refusal_is_consulted_before_callable():
    calls = []
    registry = _registry()
    registry.tool(name="sample")(lambda: calls.append("executed"))
    flags = dict(read_only=True, project_bound=True, allow_web=False, allow_location=False, unsafe=False)

    def refuse(name, **kwargs):
        calls.append((name, kwargs))
        return "project-bound execution contract"

    result = dispatch.dispatch("sample", {}, registry, (), run_refusal=refuse, **flags)
    assert result.startswith("ERROR: HOST POLICY")
    assert calls == [("sample", flags)]


def test_generated_help_uses_required_optional_and_enum_schema():
    def sample(required: str, count: int = 2, mode: Literal["read", "write"] = "read"):
        """A sample registered operation."""
    registry = _registry(sample)
    help_text = "\n".join(dispatch.generated_help_lines(registry, ()))
    assert "sample:" in help_text
    assert '"required":["required"]' in help_text
    assert '"default":2' in help_text
    assert '"enum":["read","write"]' in help_text
    assert dispatch.generated_help_lines(registry, {"sample"}) == ()


def test_server_gate_is_consulted_before_generic_dispatch(monkeypatch):
    import server

    calls = []
    monkeypatch.setattr(server, "_agent_permission_gate_error",
                        lambda name, args: calls.append(name) or "ERROR: gated")
    monkeypatch.setattr(server._generic_agent_dispatch, "dispatch",
                        lambda *a, **k: pytest.fail("fallback ran despite refusal"))
    assert server._agent_dispatch("computer_use_status", {}) == "ERROR: gated"
    assert calls == ["computer_use_status"]


def test_registered_tools_minus_explicit_exclusions_are_reachable():
    import server
    import tool_capabilities

    # Independent SDK inventory: do not derive the requirement from the same
    # helper that supplies the dispatcher capability set.
    registered = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    excluded = dispatch.excluded_names(server.mcp, server._AGENT_SYSTEM_OPERATOR_TOOLS)
    assert len(registered) >= 200
    assert registered - excluded <= tool_capabilities.dispatch_names(server._agent_dispatch)
    advertised = set(server._agent_help_advertised_tools(server._agent_tool_help(allow_location=True)))
    assert registered - excluded <= advertised


def test_newly_registered_tool_reaches_help_dispatch_and_gate(monkeypatch):
    import server
    import tool_capabilities

    calls = []
    def newly_registered_nl_tool(value: int):
        calls.append(value)
        return "ran"

    registry = _registry(newly_registered_nl_tool)
    monkeypatch.setattr(server, "mcp", registry)
    monkeypatch.setattr(server, "_agent_permission_gate_error", lambda *a, **k: "")
    assert "newly_registered_nl_tool" in tool_capabilities.dispatch_names(server._agent_dispatch)
    assert "newly_registered_nl_tool:" in server._agent_tool_help()
    assert server._agent_dispatch("newly_registered_nl_tool", {"value": 5}) == "ran"
    assert calls == [5]


@pytest.mark.parametrize("unsafe", [False, True])
def test_generic_desktop_tools_remain_local_and_existing_cloud_tools_unchanged(unsafe):
    import server
    from sonder_runtime.domain.computer_use.intent import COMPUTER_TOOLS

    for name in COMPUTER_TOOLS:
        assert server._cloud_agent_tool_policy_error(name, unsafe=unsafe)
        assert name not in server._agent_help_advertised_tools(server._agent_tool_help(cloud=True, unsafe=unsafe))
    assert not server._cloud_agent_tool_policy_error("status", unsafe=unsafe)
    assert not server._cloud_agent_tool_policy_error("web_search", unsafe=unsafe)


def test_generic_dispatch_refuses_authority_arguments_and_hides_them_from_help():
    """A model must not hand itself a token, an approval or wider roots."""
    import server
    from sonder_runtime.bootstrap import generic_agent_dispatch as g

    for name in ("vision_analyze", "fetch_artifact", "codegen_build_loop"):
        out = g.dispatch(name, {"token": "x"}, server.mcp, server._AGENT_SYSTEM_OPERATOR_TOOLS,
                         run_refusal=lambda *a, **k: "")
        assert "may not supply token" in out, out
    out = g.dispatch("vision_analyze", {"path": "a.png", "prompt": "p", "extra_roots": "C:/"},
                     server.mcp, server._AGENT_SYSTEM_OPERATOR_TOOLS, run_refusal=lambda *a, **k: "")
    assert "may not supply extra_roots" in out
    help_lines = g.generated_help_lines(server.mcp, (), server._AGENT_SYSTEM_OPERATOR_TOOLS)
    assert help_lines
    for line in help_lines:
        assert '"token"' not in line and '"approval"' not in line and '"extra_roots"' not in line
