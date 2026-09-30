"""Actual MCP registry, agent visibility, HTTP dispatcher and risk contracts."""
import ast
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import command_catalog
import permission_modes
import server


def test_live_registry_and_server_export_both_callables():
    tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    assert callable(server.playbook_note) and callable(server.playbook_read)
    assert set(tools["playbook_note"].parameters["properties"]) == {
        "topic", "category", "title", "body", "evidence", "triggers",
    }
    assert "non-obvious" in tools["playbook_note"].description
    assert permission_modes.risk_of("playbook_note") == permission_modes.risk_of("sonder_remember_fact") == "ask"
    assert permission_modes.risk_of("playbook_read") == "safe"
    assert permission_modes.risk_of("playbooks") == "dangerous"
    assert command_catalog.by_name("/playbook_note").category == "memory"


def test_local_agent_sees_tools_but_hosted_agent_does_not():
    local = server._agent_tool_help()
    assert "- playbook_note:" in local and "- playbook_read:" in local
    hosted = server._agent_tool_help(cloud=True)
    assert "- playbook_note:" not in hosted and "- playbook_read:" not in hosted


def test_http_catalogued_dispatch_finds_exported_handler():
    source = (Path(__file__).resolve().parents[1] / "sonder_runtime/interfaces/http/serve.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_dispatch_catalogued_tool")
    namespace = {
        "command_catalog": command_catalog,
        "server": SimpleNamespace(playbook_read=server.playbook_read, approved_call_reach=lambda *args: nullcontext()),
        "_run_catalogued_tool_gated": lambda line, name, kwargs, handler, **options: (name, kwargs, callable(handler)),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<http dispatch>", "exec"), namespace)
    result = namespace[function.name]("/playbook_read topic=builds", SimpleNamespace())
    assert result == ("playbook_read", {"topic": "builds"}, True)
    # The actual HTTP account-isolation declaration protects both names.
    assign = next(node for node in tree.body if isinstance(node, ast.Assign) and
                  any(isinstance(target, ast.Name) and target.id == "_ACCOUNT_GLOBAL_MEMORY_TOOLS" for target in node.targets))
    values = ast.literal_eval(assign.value.args[0])
    assert {"playbook_read", "playbook_note"} <= set(values)
