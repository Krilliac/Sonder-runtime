"""Adapt the runtime's one permission decider to the typed tool gateway.

The typed gateway asks a ``PermissionEvaluator`` whether a call may proceed.
This adapter answers with ``permission_modes`` -- the same mode matrix and
rule set every legacy surface consults -- so a typed request is never a way
around the operator's standing policy, and so there is still exactly one
decider in the process. It never prompts: a typed request has nobody at a
keyboard behind it, so the decision is the unattended one for the kind of
caller the request's scope names.

Two things distinguish the request-aware form from the narrow one:

* a request whose scope says ``gate="surface"`` was already decided by the
  in-process surface that forwards it (the console after its prompt, the
  legacy MCP and HTTP gates, the agent gate). Deciding again here would refuse
  the very call an operator just answered yes to, so the evaluator records
  ``permission:surface`` and does not decide;
* a request decided here carries its arguments to the decider, so an
  unattended refusal names the call (``call_id``) and a one-shot approval of
  exactly that call can answer the next attempt; the current effect fence,
  if a worker installed one, is consulted too.
"""
from __future__ import annotations

from typing import Any, Mapping

from ...application.tools.gateway_contract import ToolGatewayRequest, ToolPermission, ToolScope
from ...domain.common.errors import Forbidden
from ..execution import effect_fence
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

SURFACE_DECIDED = "permission:surface"


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
        return self._decide(tool_name, scope, arguments=None)

    def authorize_request(self, request: ToolGatewayRequest) -> str:
        if getattr(request.scope, "gate", "gateway") == "surface":
            return SURFACE_DECIDED
        return self._decide(request.tool_name, request.scope, arguments=dict(request.arguments))

    def _decide(self, tool_name: str, scope: ToolScope, *, arguments) -> str:
        name = self._policy_names.get(tool_name, tool_name)
        surface, exempt = SURFACES.get(getattr(scope, "source", "repl"), ("system", False))
        decision = self._policy.decide_for_caller(
            name, interactive=False, gate_control_exempt=exempt, surface=surface,
            arguments=arguments, fence=effect_fence.current(),
        )
        if decision is None:
            return "permission:exempt"
        if decision.action != self._policy.allow_action():
            error = Forbidden("permission gate refused %s: %s" % (name, decision.reason))
            error.decision = {
                "tool": name, "mode": decision.mode, "risk": decision.risk,
                "source": decision.source, "action": decision.action,
                "call_id": getattr(decision, "call_id", ""),
            }
            error.policy_match = "permission:%s" % decision.source
            raise error
        return "permission:%s" % decision.source


__all__ = ["PermissionModesEvaluator", "SURFACES", "SURFACE_DECIDED"]
