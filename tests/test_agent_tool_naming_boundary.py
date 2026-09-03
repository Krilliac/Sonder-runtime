"""Agent tool-name canonicalization lives in the domain; root names are aliases."""
import server
from sonder_runtime.domain.agents import tool_naming


def test_root_names_are_identity_preserving_aliases():
    assert server._AGENT_TOOL_ALIASES is tool_naming.AGENT_TOOL_ALIASES
    assert server._canonical_agent_tool_name is tool_naming.canonical_agent_tool_name


def test_aliases_resolve_to_registered_tools_and_unknown_names_pass_through():
    assert tool_naming.canonical_agent_tool_name("assetgen") == "artifact_generate"
    assert tool_naming.canonical_agent_tool_name("master") == "master_orchestrate"
    assert tool_naming.canonical_agent_tool_name("agent_status") == "master_status"
    assert tool_naming.canonical_agent_tool_name("file_read") == "file_read"
    assert tool_naming.canonical_agent_tool_name(None) == ""
    assert tool_naming.canonical_agent_tool_name("") == ""


def test_every_alias_target_is_itself_canonical():
    aliases = tool_naming.AGENT_TOOL_ALIASES
    assert not set(aliases.values()) & set(aliases)
    for target in aliases.values():
        assert tool_naming.canonical_agent_tool_name(target) == target
