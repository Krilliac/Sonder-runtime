"""Policy errors for tool access inside hosted agent runs."""

from __future__ import annotations


def cloud_agent_tool_policy_error(
    tool_name, *, unsafe=False, local_only_tools=(), nested_model_tools=(),
):
    """Return a policy error string, or empty string when allowed."""
    if tool_name in local_only_tools:
        return (
            "ERROR: HOST POLICY: local-only tool '%s' is disabled inside a "
            "hosted agent so private workspace or machine data cannot enter "
            "the hosted model transcript." % tool_name
        )
    if tool_name in nested_model_tools:
        if unsafe is True:
            return ""
        return (
            "ERROR: HOST POLICY: nested model-spawning tool '%s' is disabled "
            "inside a hosted agent so all hosted output remains in one "
            "bounded ledger." % tool_name
        )
    return ""
