"""Pure HTTP authority mapping contract.

The HTTP boundary supplies runtime declarations explicitly.  Keeping this
small policy function free of root-module imports lets the served adapter
enforce role bindings without importing the broader server diagnostics
contract.
"""
from __future__ import annotations

import json
import logging
logger = logging.getLogger(__name__)

SYSTEM_OPERATION_UNBOUND = "unbound-system-operation"
MAX_NESTED_SLASH_DEPTH = 8

# Slash commands handled inside ``control_command`` that bypass top-level HTTP
# account scoping (session namespacing, fanout/autopilot owners, scoped tasks,
# or global operator state) when nested inside ``/sonder`` or ``/loop``.
_ACCOUNT_UNSCOPED_NESTED_SLASH_COMMANDS = frozenset({
    "/activity", "/agent", "/agentcancel", "/agentcapacity", "/agentretry",
    "/agents", "/auto", "/autopilot", "/cancelagents", "/capacity",
    "/chainofthought", "/checklist", "/compact", "/compaction", "/context",
    "/contextsize", "/cot", "/ctxsize", "/delegate", "/discovertool", "/dump",
    "/effort", "/embedfix", "/embeddings", "/emotion", "/emotions",
    "/endreport", "/forge", "/game", "/gamecampaign", "/gamefleet", "/gamegen",
    "/gamesuite", "/goal", "/goals", "/improve", "/improvements", "/learn_tool",
    "/learnhealth", "/learning", "/learntool", "/master", "/master_orchestrate",
    "/masterstatus", "/metrics", "/mission", "/mood", "/plan", "/prefer",
    "/preference", "/preferences", "/privacy", "/privacyfix", "/privacyreview",
    "/prompts", "/quality", "/qualityfix", "/report", "/retryagent", "/run",
    "/runproject", "/stats", "/task", "/tasks", "/temp", "/thoughts", "/todo",
    "/todos", "/vectors", "/work", "/workbench_agent",
})

# Catalogued tools that require top-level HTTP scoping or read operator-global
# state and must not be invoked nested inside ``/sonder`` by a hosted account.
_ACCOUNT_UNSCOPED_NESTED_TOOLS = frozenset({
    "activity_status", "admin_private_chain_of_thought", "agent", "agent_lane",
    "autopilot_cancel", "autopilot_pause", "autopilot_resume", "autopilot_start",
    "autopilot_status", "batch_job_recent", "batch_job_results", "batch_job_status",
    "checklist_create", "checklist_show", "checklist_update",
    "context_compaction_plan", "context_health", "diagnostics",
    "emotion_vector_status", "game_reference_suite", "learn_preference",
    "learn_tiers", "master_cancel", "master_capacity", "master_status",
    "mission_start", "mission_status", "model_fanout_recent", "model_fanout_status",
    "openrouter_account", "preferences_status", "reasoning_show",
    "self_heal_check", "self_heal_repair", "set_context_size",
    "system_improvement_report", "system_profile_text", "task_create",
    "task_delete", "task_depend", "task_ledger", "task_list", "task_plan",
    "task_progress", "task_show", "task_update", "tune_emotion_vectors",
    "turn_inspect", "update_emotion_vectors", "workbench_agent",
})


def system_operation_for(
    tool_name,
    *,
    operation_tools=None,
    operator_tools=None,
    canonicalize=None,
):
    name = str(tool_name or "").strip().lstrip("/")
    if not name:
        return ""
    if canonicalize is not None:
        name = canonicalize(name)
    operation_tools = {} if operation_tools is None else operation_tools
    operator_tools = frozenset() if operator_tools is None else operator_tools
    bound = operation_tools.get(name, "")
    if bound:
        logger.debug(f"system_operation_for: tool={name!r} -> bound operation={bound!r}")
        return str(bound)
    if name in operator_tools:
        logger.debug(f"system_operation_for: tool={name!r} -> unbound system operation")
        return SYSTEM_OPERATION_UNBOUND
    return ""


_SELFMOD_COMMANDS = frozenset({"/selfmod", "/selfmodify"})
# Replacing or restoring Sonder's own source, and deleting the backups a
# rollback restores from: the same tier as ``/update apply``.
_SELFMOD_DEPLOY_ACTIONS = frozenset({"deploy", "rollback", "prune-backups"})
# Process-wide self-modification policy.
_SELFMOD_POLICY_ACTIONS = frozenset({"enable", "disable", "retention", "mode"})


def selfmod_slash_operation(command: str, action: str, *, has_value: bool) -> str:
    """The system operation a ``/selfmod`` action performs, or ``""``.

    *command* is the normalized slash head and *action* its lower-cased first
    word; *has_value* says whether anything follows the action. Reads, and
    the run stages (plan/run/approve/reject/resume/cancel), return ``""`` and
    stay at the developer tier the dangerous-slash gate already enforces. A
    bare ``mode`` only reads the setting.
    """
    if command not in _SELFMOD_COMMANDS:
        return ""
    if action in _SELFMOD_DEPLOY_ACTIONS:
        return "selfmod_deploy"
    if action in _SELFMOD_POLICY_ACTIONS and (action != "mode" or has_value):
        return "runtime_policy_change"
    return ""


def slash_system_operation(command, argument, *, slash_head):
    """Classify named slash controls that bypass catalogued tool dispatch."""
    head = slash_head(command)
    parts = str(argument or "").strip().split(None, 1)
    action = parts[0].lower() if parts else ""
    if head in ("/runtime", "/models") and action in ("set", "reset"):
        return "runtime_policy_change"
    if head in ("/runtime", "/models") and action == "tools" and (
        parts[1:] and parts[1].split()[0].lower() in ("allow", "revoke", "deny", "undeny")
    ):
        return "runtime_policy_change"
    if head in ("/runtime", "/models") and action == "overflow" and (
        parts[1:] and parts[1].split()[0].lower() not in ("status", "show", "help", "?")
    ):
        return "runtime_policy_change"
    if head in ("/update", "/updatesource") and action in ("", "apply", "now"):
        return "selfmod_deploy"
    if head in ("/stash", "/runtime-stash") and action.replace("_", "-") in ("save", "save-untracked", "pop"):
        return "selfmod_deploy"
    return selfmod_slash_operation(head, action, has_value=bool(parts[1:]))


def loop_global_operation_refusal(
    actions_json,
    context,
    *,
    loop_action_tool,
    system_operation_for,
    role_tool_refusal,
    admin_authorized,
    authority_error,
    nested_slash_refusal,
    account_global_memory_refusal=None,
    depth=0,
):
    """Refuse HTTP loop payloads that would mutate shared runtime state."""
    if context is None:
        return ""
    if depth >= MAX_NESTED_SLASH_DEPTH:
        return "refused /loop: nested loop actions exceed maximum depth"
    try:
        parsed = json.loads(actions_json)
    except (TypeError, ValueError):
        return ""
    actions = parsed.get("actions") if isinstance(parsed, dict) else parsed
    if not isinstance(actions, list):
        return ""
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type") or action.get("action") or "code").strip().lower()
        action_tool = loop_action_tool(action_type)
        if account_global_memory_refusal is not None:
            memory_error = account_global_memory_refusal(action_tool, context)
            if memory_error:
                return "refused /loop: %s" % memory_error
        operation = system_operation_for(action_tool)
        if not operation:
            role_error = role_tool_refusal(action_tool, context) if action_tool else ""
            if role_error:
                return "refused /loop: %s" % role_error
        elif operation == SYSTEM_OPERATION_UNBOUND:
            if not admin_authorized(context):
                return (
                    "refused /loop: administrator authorization is required "
                    "for an unclassified system operation"
                )
        else:
            err = authority_error(operation, context)
            if err:
                return "refused /loop: %s" % err
        if action_type == "sonder" or action_tool == "sonder":
            nested_error = nested_slash_refusal(
                action.get("prompt", ""), "/loop", context, _depth=depth + 1,
            )
            if nested_error:
                return nested_error
    return ""


def nested_slash_authority_refusal(
    prompt,
    label,
    context,
    *,
    slash_head,
    slash_operation,
    authority_error,
    dangerous_slash,
    developer_authorized,
    console_tools,
    http_slash_tools,
    narrow_branch_tools,
    catalog_unavailable,
    tool_authority_refusal,
    task_boundary_refusal,
    parse_invocation,
    project_refusal,
    loop_refusal,
    depth=0,
):
    """Enforce HTTP slash and tool authority on a nested prompt reaching control_command."""
    if context is None:
        return ""
    stripped = str(prompt or "").strip()
    if not stripped.startswith("/"):
        return ""
    if depth >= MAX_NESTED_SLASH_DEPTH:
        return "refused %s: nested slash commands exceed maximum depth" % label
    parts = stripped.split(None, 1)
    cmd = slash_head(parts[0])
    arg = parts[1] if len(parts) > 1 else ""
    operation = slash_operation(cmd, arg)
    if operation:
        err = authority_error(operation, context)
        if err:
            return "refused %s: %s" % (label, err)
    if dangerous_slash(stripped) and not developer_authorized(context):
        return "refused %s: developer or administrator authorization is required" % label
    try:
        console_map = console_tools()
        chain_tools = console_map.get(cmd, ()) or http_slash_tools().get(cmd, ())
    except catalog_unavailable as exc:
        return "refused %s: %s" % (label, exc)
    chain_tools = narrow_branch_tools(cmd, arg, chain_tools)
    if chain_tools:
        refusal = tool_authority_refusal(chain_tools, label, context=context)
        if refusal:
            return refusal
    boundary_tool = {
        "/master": "master_orchestrate", "/master_orchestrate": "master_orchestrate",
        "/work": "workbench_agent", "/agent": "workbench_agent",
        "/agentretry": "master_retry", "/retryagent": "master_retry",
    }.get(cmd)
    if boundary_tool:
        task_error = task_boundary_refusal(boundary_tool, {}, context)
        if task_error:
            return task_error
    is_account = bool((context or {}).get("account"))
    if is_account and cmd in _ACCOUNT_UNSCOPED_NESTED_SLASH_COMMANDS:
        return (
            "refused %s: nested '%s' cannot bypass top-level HTTP account scoping; "
            "issue the scoped command directly." % (label, cmd)
        )
    try:
        invocation = parse_invocation(stripped)
    except ValueError:
        if is_account and label in ("/sonder", "/loop") and cmd not in console_map:
            return (
                "refused %s: unrecognized nested command '%s'; hosted account chat "
                "must use /v1/chat/completions." % (label, cmd)
            )
        return ""
    except catalog_unavailable as exc:
        return "refused %s: %s" % (label, exc)
    if invocation:
        nested_tool, nested_kwargs = invocation
        refusal = tool_authority_refusal((nested_tool,), label, context=context)
        if refusal:
            return refusal
        task_error = task_boundary_refusal(nested_tool, nested_kwargs, context)
        if task_error:
            return task_error
        if is_account and nested_tool in _ACCOUNT_UNSCOPED_NESTED_TOOLS:
            return (
                "refused %s: nested '/%s' cannot bypass top-level HTTP account scoping; "
                "issue the scoped command directly." % (label, nested_tool)
            )
        if nested_tool in ("master_orchestrate", "sonder", "agent", "workbench_agent"):
            project_error = project_refusal(nested_kwargs.get("project", ""))
            if project_error:
                return project_error
        if nested_tool == "agent":
            cwd_error = project_refusal(nested_kwargs.get("cwd", ""))
            if cwd_error:
                return cwd_error
        if nested_tool == "loop":
            loop_error = loop_refusal(
                nested_kwargs.get("actions_json"), context, _depth=depth + 1,
            )
            if loop_error:
                return loop_error
        elif nested_tool == "sonder":
            nested_refusal = nested_slash_authority_refusal(
                nested_kwargs.get("prompt", ""), label, context,
                slash_head=slash_head, slash_operation=slash_operation,
                authority_error=authority_error, dangerous_slash=dangerous_slash,
                developer_authorized=developer_authorized, console_tools=console_tools,
                http_slash_tools=http_slash_tools, narrow_branch_tools=narrow_branch_tools,
                catalog_unavailable=catalog_unavailable, tool_authority_refusal=tool_authority_refusal,
                task_boundary_refusal=task_boundary_refusal, parse_invocation=parse_invocation,
                project_refusal=project_refusal, loop_refusal=loop_refusal, depth=depth + 1,
            )
            if nested_refusal:
                return nested_refusal
            if is_account:
                nested_prompt = str(nested_kwargs.get("prompt", "") or "").strip()
                if (
                    not nested_prompt.startswith("/")
                    or str(nested_kwargs.get("tier") or "").strip()
                    or str(nested_kwargs.get("model_override") or "").strip()
                ):
                    return (
                        "refused %s: hosted account chat must use /v1/chat/completions "
                        "rather than the local operator /sonder tool." % label
                    )
    elif is_account and label in ("/sonder", "/loop") and cmd not in console_map:
        return (
            "refused %s: unrecognized nested command '%s'; hosted account chat "
            "must use /v1/chat/completions." % (label, cmd)
        )
    return ""


__all__ = [
    "MAX_NESTED_SLASH_DEPTH",
    "SYSTEM_OPERATION_UNBOUND",
    "loop_global_operation_refusal",
    "nested_slash_authority_refusal",
    "selfmod_slash_operation",
    "slash_system_operation",
    "system_operation_for",
]
