"""Pure canonicalization of agent tool spellings.

Short aliases such as ``assetgen`` or ``master`` resolve to the registered
tool name before any policy or dispatch decision looks at the spelling, so
every gate sees one canonical vocabulary. It is explicit-input and
side-effect free. Moved from ``server.py`` in the WP1 Three-Hundred-Fourth
Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations


AGENT_TOOL_ALIASES = {
    "assetgen": "artifact_generate",
    "game_generate": "game_generate_and_test",
    "game_campaign": "game_generation_campaign",
    "improvement_report": "system_improvement_report",
    "agent_status": "master_status",
    "agent_capacity": "master_capacity",
    "agent_cancel": "master_cancel",
    "agent_retry": "master_retry",
    "master": "master_orchestrate",
}


def canonical_agent_tool_name(tool_name):
    name = str(tool_name or "")
    return AGENT_TOOL_ALIASES.get(name, name)
