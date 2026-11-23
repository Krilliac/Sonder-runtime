"""tool_contract -- the cross-surface authority contract, derived not restated.

Sonder's judgement about which tools are shared *system operations* lives in
several declarations that historically only agreed by discipline:
``sonder_serve.SYSTEM_OPERATION_TOOLS`` (the HTTP role bindings),
``server._AGENT_SYSTEM_OPERATOR_TOOLS`` (the agent-path refusals), and --
until this module replaced it -- a third hand-kept copy for ``/loop``
payloads. Every gap between them was an authority bypass wearing a different
spelling: the map for loop actions named 4 of the 7 loop-reachable system
operations, so ``{"type": "memory_privacy_repair"}`` ran for a served account
that ``/memory_privacy_repair`` refuses.

This module answers ONE question for the HTTP gates -- *which role-gated
operation does this tool perform?* -- from those declarations, with two
properties the copies never had:

* **Spellings are canonicalized first** (``server._canonical_agent_tool_name``),
  so an alias can never grade differently from its target.
* **Drift fails closed.** A tool the runtime itself declares to be a system
  operation (membership in ``_AGENT_SYSTEM_OPERATOR_TOOLS``) that has no
  declared role binding answers ``SYSTEM_OPERATION_UNBOUND``, which callers
  must treat as the strictest (administrator) operation -- never as "no
  operation". Registration or policy metadata drifting apart therefore
  *narrows* what a served account can reach instead of widening it.

Like ``tool_capabilities``, importing this module never registers, allows,
denies, or runs a tool: enforcement stays in the gates
(``sonder_serve._http_tool_refusal`` and ``_loop_global_operation_refusal``
consult it; the console, direct MCP, and the agent path keep their own
gates). Unlike ``tool_capabilities`` it is not a shadow: the HTTP boundary
reads it on every served dispatch.

Stdlib only. ``server`` and ``sonder_serve`` are imported lazily inside
functions: ``sonder_serve`` imports this module at the top level, and
``server`` imports ``sonder_serve``-adjacent modules during startup, so a
module-level import here would be circular.
"""
from __future__ import annotations

from dataclasses import dataclass

# The deny-by-default answer for a declared-but-unbound system operation.
# Deliberately not a member of SYSTEM_OPERATION_ROLES: an unbound name has no
# curated role, and mapping it to one implicitly would hide the drift this
# sentinel exists to surface. Callers refuse unless the caller holds
# administrator authority, and the remedy is to declare the binding.
SYSTEM_OPERATION_UNBOUND = "unbound-system-operation"


def _server():
    import server

    return server


def _serve():
    import sonder_runtime.interfaces.http.serve as sonder_serve

    return sonder_serve


def system_operation_for(
    tool_name,
    *,
    operation_tools=None,
    operator_tools=None,
    canonicalize=None,
):
    """The role-gated operation ``tool_name`` performs, or ``""``.

    Returns, in order of precedence:

    * the declared binding from ``SYSTEM_OPERATION_TOOLS`` when one exists;
    * ``SYSTEM_OPERATION_UNBOUND`` when the runtime declares the tool a
      system operation (``_AGENT_SYSTEM_OPERATOR_TOOLS``) but no binding
      says which role may perform it over HTTP;
    * ``""`` for everything else -- ordinary tools stay governed by the
      permission mode, rules, and their own contracts.

    The keyword seams exist for the conformance tests, which inject mutated
    declarations to prove drift is *detected* without editing live policy;
    production callers pass only the tool name.
    """
    name = str(tool_name or "").strip().lstrip("/")
    if not name:
        return ""
    if canonicalize is None:
        canonicalize = _server()._canonical_agent_tool_name
    name = canonicalize(name)
    if operation_tools is None:
        operation_tools = _serve().SYSTEM_OPERATION_TOOLS
    if operator_tools is None:
        operator_tools = _server()._AGENT_SYSTEM_OPERATOR_TOOLS
    bound = operation_tools.get(name, "")
    if bound:
        return str(bound)
    if name in operator_tools:
        return SYSTEM_OPERATION_UNBOUND
    return ""


@dataclass(frozen=True)
class ToolContract:
    """One tool's cross-surface authority contract, derived at call time.

    ``http_role`` is the EFFECTIVE role a served caller needs at the HTTP
    boundary, not merely the declared one: an unbound system operation
    answers ``admin`` because that is what the deny-by-default rule enforces.
    ``""`` means the tool carries no authority boundary of its own and is
    governed by the permission mode, rules, and its own body.
    """

    name: str
    registered: bool
    risk: str
    http_operation: str
    http_role: str
    agent_operator: bool
    durable_authority: bool


def _registered_names(server):
    try:
        return frozenset(
            tool.name for tool in server.mcp._tool_manager.list_tools()
        )
    except Exception:
        # A registry that cannot be read yields an empty *registered* set;
        # contracts() still reports the declared names, and the permission
        # gate itself fails closed on a blind catalog (risk_of returns
        # UNCLASSIFIED there), so this never widens anything.
        return frozenset()


def contracts():
    """Every registered or declared tool name, mapped to its contract."""
    import permission_modes

    server = _server()
    serve = _serve()
    registered = _registered_names(server)
    operator = frozenset(server._AGENT_SYSTEM_OPERATOR_TOOLS)
    durable = frozenset(permission_modes.DURABLE_AUTHORITY_TOOLS)
    loop_tools = frozenset(
        server._loop_action_tool(action) for action in server._LOOP_ACTION_TYPES
    )
    rows = {}
    for name in sorted(registered | operator | durable | loop_tools):
        operation = system_operation_for(name)
        if operation == SYSTEM_OPERATION_UNBOUND:
            role = "admin"
        else:
            role = serve.SYSTEM_OPERATION_ROLES.get(operation, "") if operation else ""
        rows[name] = ToolContract(
            name=name,
            registered=name in registered,
            risk=permission_modes.risk_of(name),
            http_operation=operation,
            http_role=role,
            agent_operator=name in operator,
            durable_authority=name in durable,
        )
    return rows


def validate_contracts(
    *,
    operation_tools=None,
    operator_tools=None,
    operation_roles=None,
    durable_tools=None,
    registered_names=None,
):
    """Structural drift between the authority declarations, as data.

    Deliberately the same shape as ``tool_capabilities.validate_shadow``:
    deterministic strings a test or diagnostics surface can print, computed
    from the live declarations unless a mutated set is injected (which is how
    the conformance tests prove the detector can fail).

    The unbound check exempts durable-authority tools: those are refused
    non-interactively on every surface by ``decide()`` itself, which is
    stricter than any role binding, and binding them anyway would replace
    that refusal's actionable remedy text with a generic role message.
    """
    import permission_modes

    server = _server()
    serve = _serve()
    if operation_tools is None:
        operation_tools = serve.SYSTEM_OPERATION_TOOLS
    if operator_tools is None:
        operator_tools = frozenset(server._AGENT_SYSTEM_OPERATOR_TOOLS)
    if operation_roles is None:
        operation_roles = serve.SYSTEM_OPERATION_ROLES
    if durable_tools is None:
        durable_tools = frozenset(permission_modes.DURABLE_AUTHORITY_TOOLS)
    if registered_names is None:
        registered_names = _registered_names(server)

    issues = []
    for tool, operation in operation_tools.items():
        if operation not in operation_roles:
            issues.append(
                "%s: bound to %r, which is not a declared system operation"
                % (tool, operation)
            )
        if tool in registered_names and tool not in operator_tools:
            issues.append(
                "%s: role-bound over HTTP but the agent path does not refuse "
                "it as a system operation" % tool
            )
    for tool in operator_tools:
        if tool not in registered_names:
            continue
        if tool in operation_tools or tool in durable_tools:
            continue
        issues.append(
            "%s: declared a system operation with no HTTP role binding; the "
            "deny-by-default rule holds it admin-only, but the declaration "
            "is drift -- bind it to an operation" % tool
        )
    for tool in durable_tools:
        if tool in registered_names and tool not in operator_tools:
            issues.append(
                "%s: grants durable authority but the agent path does not "
                "refuse it as a system operation" % tool
            )
    return tuple(sorted(issues))
