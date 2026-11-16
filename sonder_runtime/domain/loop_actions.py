"""Pure resolution of loop actions to the tools that actually run.

The loop dispatcher accepts a few action names that are not tool names; the
permission gate must decide on the tool that really runs, and a verdict
result is only ok when the output starts with the action's success prefix.
The base text-result builder is injected by the caller. Moved from
``server.py`` in the WP1 Three-Hundred-Fifteenth Slice with its behaviour
byte-for-byte intact.
"""
from __future__ import annotations

from sonder_runtime.domain.agents.tool_naming import canonical_agent_tool_name


# `_loop_dispatch` action types are mostly tool names already; these are the
# ones that are not, so the gate below decides on the tool that actually runs
# rather than on a name `risk_of` has never heard of.
LOOP_ACTION_TOOLS = {
    "code": "run_code",
    "project": "run_project",
    "artifact": "artifact_generate",
    "artifact_check": "artifact_ground",
    "game_reference": "game_reference_suite",
    "game": "game_generate_and_test",
    "work": "workbench_agent",
    "agent": "workbench_agent",
    "improvement_report": "system_improvement_report",
    "profile_status": "system_profile_text",
    "emotion_status": "emotion_vector_status",
    "emotion_update": "update_emotion_vectors",
    "emotion_tune": "tune_emotion_vectors",
    "learning_health": "learning_health_status",
}


def loop_action_tool(action_type):
    """The tool a loop action really runs, for the permission gate."""
    name = str(action_type or "").strip().lower()
    return canonical_agent_tool_name(LOOP_ACTION_TOOLS.get(name, name))


def loop_verdict_result(action_type, text, success_prefix, *, text_result):
    """Mark a loop text result ok only when it starts with the success prefix.

    ``text_result(action_type, text)`` builds the base result; it is injected
    because its ``ERROR:`` prefix parse is recorded in the shrink-only
    error-signal baseline under its current scope.
    """
    result = text_result(action_type, text)
    result["ok"] = bool(text) and text.startswith(success_prefix)
    return result
