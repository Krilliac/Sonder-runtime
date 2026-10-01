"""Registrar for pure agent help and registrar discovery utilities."""
from __future__ import annotations

import importlib
import pkgutil
import time
from typing import Any

from ..domain.agents.tool_help import advertised_scope, tool_help_text
from ..domain.cloud_access import LEGACY_ERROR_PREFIX


def help_scope(mcp, allowed, refusal, **flags):
    names = catalog_from_mcp(mcp)
    return advertised_scope(name for name in names
                            if (allowed is None or name in allowed) and not refusal(name, **flags))


# The specialized agent dispatcher historically accepts these spellings in
# addition to the public MCP schema. Keep those existing calls valid when
# normalizing; dispatch remains responsible for its JSON-list translations.
_DISPATCH_ARGUMENTS = {
    "workspace_run": {"args": "args_json"}, "script_run": {"args": "args_json"},
    "context_pack": {"paths": "paths_json"},
    "file_batch_write": {"operations": "operations_json"}, "json_patch": {"operations": "operations_json"},
    "data_convert": {"fields": "fields_json"},
    "run_project": {"commands": "commands_json", "files": "files_json"},
    "sqlite_mutate": {"parameters": "parameters_json"},
    "update_emotion_vectors": {"vectors": "vectors_json"},
    "memory_privacy_repair": {"lesson_ids": "lesson_ids_json"},
    "ground_artifact": {"checks": "checks_json"},
    "archive_create": {"inputs": "inputs_json", "format": "archive_format"},
    "file_read_range": {"start": "start_line", "end": "end_line"},
    "command_registry_list": {"filter": "filter_text"}, "permission_policy": {"tool": "tool_name"},
    "master_cancel": {"selector": "agent_id"}, "master_retry": {"selector": "agent_id"},
    "master_capacity": {"agents": "requested_agents"},
    "task_delete": {"id": "task_id"}, "task_depend": {"id": "task_id"},
    "task_ledger": {"id": "goal_id"}, "task_show": {"id": "task_id"}, "task_update": {"id": "task_id"},
    "checklist_show": {"id": "checklist_id"}, "checklist_update": {"id": "checklist_id", "item_id": "item_id"},
    "task_plan": {"items": "steps"}, "artifact_ground": {"requirements": "requirements_json"},
    "artifact_generate": {"prompt": "brief"}, "tune_emotion_vectors": {"text": "feedback_text"},
    **{name: {"root": "path"} for name in (
        "directory_tree", "workspace_inventory", "dependency_inventory", "directory_digest",
        "project_detect", "repository_symbol_index")},
}


def argument_schema(mcp, name):
    """Return schema plus already-supported dispatcher aliases, without I/O."""
    schema = catalog_from_mcp(mcp).get(name, {}).get("input_schema")
    if not isinstance(schema, dict):
        return None
    schema = dict(schema)
    props = dict(schema.get("properties", {}))
    for alias, canonical in _DISPATCH_ARGUMENTS.get(name, {}).items():
        props.setdefault(alias, {} if canonical.endswith("_json") else props.get(canonical, {}))
    schema["properties"] = props
    return schema


def catalog_from_mcp(mcp):
    """Extract advertised names and schemas from the live MCP registry."""
    tools = getattr(getattr(mcp, "_tool_manager", None), "_tools", {})
    result = {}
    for name, tool in (tools.items() if isinstance(tools, dict) else ()):
        fn = getattr(tool, "fn", tool)
        schema = (getattr(tool, "parameters", None) or getattr(tool, "input_schema", None)
                  or getattr(tool, "inputSchema", None))
        result[str(name)] = {"description": str(getattr(tool, "description", "") or getattr(fn, "__doc__", "") or "").strip(), "input_schema": schema or {}}
    return result


def catalog_for(mcp, advertised=None):
    catalog = catalog_from_mcp(mcp)
    if advertised is None:
        return catalog
    allowed = {str(name) for name in advertised}
    return {name: item for name, item in catalog.items() if name in allowed}


def dispatch_tool_help(mcp, args, advertised=None):
    """Pure dispatcher helper; ``args`` must select exactly one query/name."""
    args = args if isinstance(args, dict) else {}
    return tool_help_text(name=args.get("name", ""), query=args.get("query", ""),
                          catalog=catalog_for(mcp, advertised))


def discover_agent_tool_registrars(package: str = "sonder_runtime.bootstrap"):
    """Return ``register`` callables from every ``*_agent_tools`` module.

    Discovery is intentionally metadata-only: importing a registrar must not
    probe the host or execute a tool body. The returned tuple is stable and
    sorted so server wiring and drift tests observe the same surface.
    """
    pkg = importlib.import_module(package)
    found = []
    for info in pkgutil.iter_modules(getattr(pkg, "__path__", ())):
        # ``agent_help_tools`` predates the conventional ``*_agent_tools``
        # suffix; include it explicitly so the registrar owns its own tool.
        if info.name != "agent_help_tools" and not info.name.endswith("_agent_tools"):
            continue
        module = importlib.import_module(f"{package}.{info.name}")
        registrar = getattr(module, "register", None)
        if callable(registrar):
            found.append((info.name, registrar))
    return tuple(sorted(found, key=lambda item: item[0]))


def register(mcp, record) -> None:
    """Expose ``tool_help`` as a bounded, read-only MCP tool."""
    def run(args: dict[str, Any]) -> str:
        started = time.time()
        try:
            output = dispatch_tool_help(mcp, args)
        except Exception as exc:  # tool failures are returned, never escape MCP
            output = (f"{LEGACY_ERROR_PREFIX} tool_help: {exc}")[:1200]
        if callable(record):
            record("tool_help", args, ok=not output.startswith(LEGACY_ERROR_PREFIX), started=started, summary=output[:200], output=output)
        return output

    @mcp.tool()
    def tool_help(name: str = "", query: str = "") -> str:
        """Describe one advertised agent tool, or find up to five by query."""
        return run({"name": name, "query": query})


__all__ = ["catalog_for", "catalog_from_mcp", "dispatch_tool_help", "discover_agent_tool_registrars", "register"]
