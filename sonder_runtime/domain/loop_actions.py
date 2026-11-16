"""Pure resolution of loop actions to the tools that actually run.

The loop dispatcher accepts a few action names that are not tool names; the
permission gate must decide on the tool that really runs, and a verdict
result is only ok when the output starts with the action's success prefix.
The base text-result builder is injected by the caller. Moved from
``server.py`` in the WP1 Three-Hundred-Fifteenth Slice with its behaviour
byte-for-byte intact.
"""
from __future__ import annotations

import contextlib
import contextvars

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


# Set by a surface that has authenticated an operator (the console, local-open
# HTTP, an administrator account) around a `loop`/`workflow_run` call it
# serves.  Without it `_loop_dispatch` treats the actions as model-authored.
_LOOP_OPERATOR_AUTHORITY = contextvars.ContextVar("loop_operator_authority", default=False)


@contextlib.contextmanager
def loop_operator_authority():
    """Mark loop actions dispatched inside this block as operator-authored."""
    token = _LOOP_OPERATOR_AUTHORITY.set(True)
    try:
        yield
    finally:
        _LOOP_OPERATOR_AUTHORITY.reset(token)


def loop_operator_tool_refusal(action_type, action_types, operator_tools):
    """Refusal result for a loop action whose tool is operator-only, or None.

    The agent path refuses everything in ``operator_tools``; a loop action is
    chosen by the same model, so the permission mode alone (which
    `learn_preference` passes in the default mode) is not enough.  Unknown
    action types keep the dispatcher's canonical vocabulary error.
    """
    name = str(action_type or "").strip().lower()
    if name not in action_types or _LOOP_OPERATOR_AUTHORITY.get():
        return None
    tool = loop_action_tool(name)
    if tool not in operator_tools:
        return None
    return {
        "ok": False,
        "type": str(action_type or ""),
        "summary": "refused by the permission gate (operator-only tool)",
        "output": (
            "HOST POLICY: '%s' is refused: tool '%s' is a system operation "
            "and cannot be run by a loop action. Use an authenticated "
            "developer/admin surface with explicit operator intent. This is "
            "standing policy, not a transient failure -- choose a different "
            "action." % (action_type, tool)
        ),
    }
