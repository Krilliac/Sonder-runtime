"""Server contracts for opt-in bounded process-risk observation."""
from __future__ import annotations

import json

import process_risk
import server


def test_process_tools_are_default_disabled(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.delenv(process_risk.OPT_IN_ENV, raising=False)
    assert json.loads(server.process_list())["status"] == "opt_in_required"
    assert json.loads(server.process_memory_risk_inspect(1234))["status"] == "opt_in_required"


def test_general_agent_can_use_opt_in_tools_but_repository_agent_cannot(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        server.process_risk_module, "list_processes",
        lambda **kwargs: {"ok": True, "status": "complete", "process_count": 1,
                          "processes": [{"pid": 42, "parent_pid": 1,
                                         "name": "fixture.exe", "thread_count": 1}]},
    )

    general = server._agent_dispatch("process_list", {}, read_only=False)
    denied = server._agent_dispatch("process_list", {}, read_only=True)

    assert json.loads(general)["processes"][0]["pid"] == 42
    assert denied.startswith("ERROR:")
    assert "process_list" in server._PROJECT_BOUND_AGENT_TOOLS
    assert "process_memory_risk_inspect" in server._PROJECT_BOUND_AGENT_TOOLS


def test_memory_result_is_content_free_through_server(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        server.process_risk_module, "inspect_process_memory",
        lambda pid, **kwargs: {
            "ok": True, "status": "bounded", "pid": pid, "risk": "high",
            "indicators": ["remote_thread_primitive"],
            "indicator_counts": {"remote_thread_primitive": 1},
            "bytes_scanned": 4096, "regions_examined": 2, "regions_read": 1,
            "timed_out": False, "limits": kwargs,
        },
    )
    output = server.process_memory_risk_inspect(4321)
    assert json.loads(output)["risk"] == "high"
    for forbidden in ("address", "command_line", "memory_bytes", "raw", "secret"):
        assert forbidden not in output.lower()


def test_process_tool_registration_help_reload_and_autopilot():
    for name in ("process_list", "process_memory_risk_inspect"):
        assert server.mcp._tool_manager.get_tool(name) is not None
        assert name in server.AGENT_TOOL_HELP
        assert name in server._WORK_INSPECTION_TOOLS
        assert name in server._AGENT_DEDUPLICATED_INSPECTION_TOOLS
        assert name in server._AUTOPILOT_OBSERVE_TOOLS
        assert name not in server.REPOSITORY_READ_ONLY_TOOLS
        assert server._autopilot_tool_policy({"policy": "observe"})(name, {"pid": 1234}) == ""
    assert "process_risk" in server.LIVE_RELOAD_MODULES
    assert "process_list/process_memory_risk_inspect" in server.tool_manifest()


def test_activity_commands_expose_only_pid_or_cap():
    assert server._agent_activity_command(
        "process_memory_risk_inspect", {"pid": 321},
    ) == "pid=321"
    assert server._agent_activity_command(
        "process_list", {"max_processes": 10},
    ) == "max_processes=10"
