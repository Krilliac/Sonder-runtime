"""Registry-backed terminal adapter, entered after the agent permission gate.

Specialized dispatch branches retain their translation and containment. New
tools use the MCP registry's callable and strict argument model; observations
never supply schemas, authority, or executable callables.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Iterable
from typing import Any

from ..domain.cloud_access import LEGACY_ERROR_PREFIX

# Existing specialized adapters for offload, master and agent_lane remain
# authoritative. The generic path must never create recursive agent runs.
RECURSIVE_ENTRYPOINTS = frozenset({
    "sonder", "agent", "workbench_agent", "loop", "offload",
    "master_orchestrate", "master_retry", "master_cancel", "master",
    "workflow_run", "agent_lane",
})
ADMIN_NAMES = frozenset({
    "elevate", "permission_mode", "permission_rule_set", "permission_approve",
    "runtime_policy_update", "runtime_source_update", "update_system_profile",
})
# Arguments that carry or widen authority on the legacy MCP surface: a
# developer token, an approval, reach beyond the configured roots. The
# hand-written branches never pass them through, so the fallback refuses them
# and the generated help never advertises them.
AUTHORITY_ARGUMENTS = frozenset({"token", "approval", "extra_roots"})


def registered_tools(registry: Any) -> dict[str, Any]:
    """Use the same materialized registry as MCP clients, including tool aliases."""
    return {tool.name: tool for tool in registry._tool_manager.list_tools()}


def excluded_names(registry: Any, system_operator_tools: Iterable[str] = ()) -> frozenset[str]:
    return (RECURSIVE_ENTRYPOINTS | ADMIN_NAMES | frozenset(system_operator_tools)
            | frozenset(name for name in registered_tools(registry) if name.startswith("admin_")))


def reachable_names(registry: Any, system_operator_tools: Iterable[str] = ()) -> frozenset[str]:
    return frozenset(registered_tools(registry)) - excluded_names(registry, system_operator_tools)


def generic_only_names(registry: Any, system_operator_tools: Iterable[str], literal_names: Iterable[str]) -> frozenset[str]:
    # Newly exposed tools default to local agents. Existing explicit cloud
    # adapters and their policy are unaffected by this fallback.
    return reachable_names(registry, system_operator_tools) - frozenset(literal_names)


def generated_help_lines(
    registry: Any, existing: Iterable[str], system_operator_tools: Iterable[str] = (),
) -> tuple[str, ...]:
    missing = reachable_names(registry, system_operator_tools) - frozenset(existing)
    lines = []
    for name, tool in sorted(registered_tools(registry).items()):
        if name in missing:
            schema = json.dumps(_advertised_schema(tool.parameters), ensure_ascii=False, separators=(",", ":"))
            summary = " ".join((tool.description or "Registered MCP tool").splitlines()[:1])
            lines.append(f"- {name}: input_schema={schema} -- {summary}")
    return tuple(lines)


def _advertised_schema(parameters: Any) -> Any:
    if not isinstance(parameters, dict):
        return parameters
    shown = dict(parameters)
    properties = shown.get("properties")
    if isinstance(properties, dict):
        shown["properties"] = {k: v for k, v in properties.items() if k not in AUTHORITY_ARGUMENTS}
    if isinstance(shown.get("required"), list):
        shown["required"] = [k for k in shown["required"] if k not in AUTHORITY_ARGUMENTS]
    return shown


def _validate_arguments(tool: Any, args: dict) -> str:
    metadata = getattr(tool, "fn_metadata", None)
    validator = getattr(getattr(metadata, "arg_model", None), "model_validate", None)
    if not callable(validator) or not isinstance(getattr(tool, "parameters", None), dict):
        return f"{LEGACY_ERROR_PREFIX} registered tool schema is unavailable; refusing generic dispatch"
    try:
        # SDK models may ignore unknown fields. Python binding forbids them;
        # strict validation rejects coercion, nested wrong types and enums.
        inspect.signature(tool.fn).bind(**args)
        validator(args, strict=True)
    except (TypeError, ValueError) as exc:
        return f"{LEGACY_ERROR_PREFIX} invalid arguments for '{tool.name}': {exc}"
    return ""


def dispatch(
    tool_name: str, args: dict, registry: Any, system_operator_tools: Iterable[str],
    *, run_refusal, read_only=False, project_bound=False, allow_web=True,
    allow_location=False, unsafe=False,
) -> Any:
    """Invoke a registered tool after host gate and per-run policy admission."""
    if tool_name in excluded_names(registry, system_operator_tools):
        return f"{LEGACY_ERROR_PREFIX} HOST POLICY: tool '{tool_name}' is excluded from generic agent dispatch."
    refusal = run_refusal(
        tool_name, read_only=read_only, project_bound=project_bound,
        allow_web=allow_web, allow_location=allow_location, unsafe=unsafe,
    )
    if refusal:
        return f"{LEGACY_ERROR_PREFIX} HOST POLICY: tool '{tool_name}' requires {refusal}."
    if not isinstance(args, dict):
        return f"{LEGACY_ERROR_PREFIX} tool args must be a JSON object"
    supplied_authority = sorted(AUTHORITY_ARGUMENTS & set(args))
    if supplied_authority:
        return (f"{LEGACY_ERROR_PREFIX} HOST POLICY: agents may not supply "
                f"{', '.join(supplied_authority)}; reach beyond the configured roots is "
                "approved at the console.")
    tool = registered_tools(registry).get(tool_name)
    if tool is None:
        return f"{LEGACY_ERROR_PREFIX} unknown tool '{tool_name}'."
    invalid = _validate_arguments(tool, args)
    if invalid:
        return invalid
    # The agent loop is synchronous; never invoke or leak an async coroutine
    # when the caller already has an event loop on this thread.
    if tool.is_async:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            return f"{LEGACY_ERROR_PREFIX} async agent tool cannot run inside an active event loop"
    try:
        result = tool.fn(**args)
        return asyncio.run(result) if tool.is_async else result
    except Exception as exc:
        return f"{LEGACY_ERROR_PREFIX} tool '{tool_name}' failed: {exc}"
