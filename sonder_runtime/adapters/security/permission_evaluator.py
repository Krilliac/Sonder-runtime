"""Adapt the runtime's one permission decider to the typed tool gateway.

The typed gateway asks a ``PermissionEvaluator`` whether a call may proceed.
This adapter answers with ``permission_modes`` -- the same mode matrix and
rule set every legacy surface consults -- so a typed request is never a way
around the operator's standing policy, and so there is still exactly one
decider in the process. It never prompts: a typed request has nobody at a
keyboard behind it, so the decision is the unattended one for the kind of
caller the request's scope names.
"""
from __future__ import annotations

from typing import Any, Mapping

from ...application.tools.gateway_contract import ToolPermission, ToolScope
from ...domain.common.errors import Forbidden
from .permission_policy import permission_policy

# The surface each request source is decided for, and whether that kind of
# caller keeps the gate-control exemption: a person driving Sonder (console,
# app) must keep a way out of ``plan``; Sonder driving itself (a worker, the
# system) and a native MCP client must not lift their own restraint.
SURFACES: Mapping[str, tuple[str, bool]] = {
    "mcp": ("native-mcp", False),
    "repl": ("repl", True),
    "http": ("http", True),
    "worker": ("loop", False),
    "system": ("system", False),
}


class PermissionModesEvaluator:
    """``PermissionEvaluator`` over ``permission_policy.decide_for_caller``.

    ``policy_names`` maps a typed (canonical) tool name to the name the
    permission catalog grades -- ``read_file`` is graded as ``file_read`` --
    so a renamed descriptor cannot slip past the catalog as unclassified.
    """

    def __init__(self, policy: Any = None, *,
                 policy_names: Mapping[str, str] | None = None) -> None:
        self._policy = policy if policy is not None else permission_policy
        self._policy_names = dict(policy_names or {})

    def authorize(self, tool_name: str, scope: ToolScope, permission: ToolPermission) -> str:
        del permission  # effects are the resource policy's business
        name = self._policy_names.get(tool_name, tool_name)
        surface, exempt = SURFACES.get(getattr(scope, "source", "repl"), ("system", False))
        decision = self._policy.decide_for_caller(
            name, interactive=False, gate_control_exempt=exempt, surface=surface,
        )
        if decision is None:
            return "permission:exempt"
        if decision.action != self._policy.allow_action():
            error = Forbidden("permission gate refused %s: %s" % (name, decision.reason))
            error.decision = {
                "tool": name, "mode": decision.mode, "risk": decision.risk,
                "source": decision.source, "action": decision.action,
            }
            error.policy_match = "permission:%s" % decision.source
            raise error
        return "permission:%s" % decision.source


__all__ = ["PermissionModesEvaluator", "SURFACES"]
