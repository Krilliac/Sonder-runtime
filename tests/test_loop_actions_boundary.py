"""Loop action resolution lives in the domain; root names stay aliases or delegates."""
import server
from sonder_runtime.domain import loop_actions


def test_root_names_are_identity_preserving_aliases():
    assert server._LOOP_ACTION_TOOLS is loop_actions.LOOP_ACTION_TOOLS
    assert server._loop_action_tool is loop_actions.loop_action_tool


def test_loop_action_tool_resolves_to_the_tool_that_actually_runs():
    assert loop_actions.loop_action_tool("code") == "run_code"
    assert loop_actions.loop_action_tool(" Work ") == "workbench_agent"
    assert loop_actions.loop_action_tool("assetgen") == "artifact_generate"
    assert loop_actions.loop_action_tool("file_read") == "file_read"
    assert loop_actions.loop_action_tool(None) == ""


def test_verdict_result_wraps_the_injected_text_result():
    def text_result(action_type, text):
        return {"type": action_type, "output": text}

    ok = loop_actions.loop_verdict_result("code", "PASS: done", "PASS", text_result=text_result)
    assert ok == {"type": "code", "output": "PASS: done", "ok": True}
    assert loop_actions.loop_verdict_result("code", "", "PASS", text_result=text_result)["ok"] is False
    assert loop_actions.loop_verdict_result("code", "FAIL", "PASS", text_result=text_result)["ok"] is False


def test_root_delegate_builds_on_the_server_text_result():
    result = server._loop_verdict_result("code", "OK build\nmore", "OK")
    assert result["ok"] is True
    assert result["type"] == "code"
    assert result["summary"] == "OK build"
    assert result["output"] == "OK build\nmore"
    assert server._loop_verdict_result("code", "ERROR: no", "OK")["ok"] is False
