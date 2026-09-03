"""Agent evidence quality and verifier reach live in the domain; root names stay compatible."""
import server
from sonder_runtime.domain.agents import evidence_quality, verification_reach


def test_root_names_are_aliases_and_the_constant_is_unchanged():
    assert server._ensemble_codegen_build_succeeded is evidence_quality.codegen_build_succeeded
    assert server._AGENT_VERIFICATION_TOOLS is verification_reach.VERIFICATION_TOOLS
    assert verification_reach.VERIFICATION_TOOLS == frozenset({"test_run", "build_run", "lint_run", "typecheck_run"})


def test_codegen_build_verdict_reads_only_the_host_rendered_terminal_lines():
    ok = evidence_quality.codegen_build_succeeded
    assert ok("compiling...\nBUILD SUCCEEDED\n")
    assert not ok("BUILD SUCCEEDED\nBUILD FAILED: link error")
    assert not ok("BUILD SUCCEEDED\nBUILD DID NOT RUN")
    assert not ok("BUILD SUCCEEDED\nBUILD MEASUREMENT INCOMPLETE (timeout)")
    assert not ok("the model said BUILD SUCCEEDED inside prose")
    assert not ok(None)


def test_tool_observation_quality_uses_the_injected_generic_predicate():
    calls = []

    def generic(observation):
        calls.append(observation)
        return observation != "bad"

    def check(tool, observation):
        return evidence_quality.tool_observation_ok(tool, observation, observation_ok=generic)

    assert check("file_read", "contents") is True
    assert check("file_read", "bad") is False
    assert check("ensemble_codegen_build_loop", "BUILD SUCCEEDED") is True
    assert check("web_fetch", None) is False
    assert check("web_fetch", "   \n") is False
    assert check("web_fetch", "<p>hello</p>") is True
    assert check("archive_list", '{"valid": true}') is True
    assert check("archive_list", '{"valid": false}') is False
    assert check("archive_list", "not json") is False
    assert "BUILD SUCCEEDED" not in calls


def test_root_wrapper_uses_the_server_generic_predicate(monkeypatch):
    monkeypatch.setattr(server, "_agent_observation_ok", lambda observation: observation == "fine")
    assert server._agent_tool_observation_ok("file_read", "fine") is True
    assert server._agent_tool_observation_ok("file_read", "other") is False


def test_verifier_reach_is_read_from_the_lane_gates():
    reach = verification_reach.verifier_reachable
    tools = frozenset({"file_read", "text_search"})
    assert reach(False, None, read_only_tools=tools) is True
    assert reach(True, None, read_only_tools=tools) is False
    assert reach(True, None, read_only_tools=tools | {"test_run"}) is True
    assert reach(False, ["file_read"], read_only_tools=tools) is False
    assert reach(False, ["file_read", "lint_run"], read_only_tools=tools) is True
    assert reach(True, ["test_run"], read_only_tools=tools) is False
    assert server._agent_verifier_reachable(True, None) is False
    assert server._agent_verifier_reachable(False, None) is True
