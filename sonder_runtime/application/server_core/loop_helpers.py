"""Pure argument builders and result formatters for ``_loop_dispatch``."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

_ACTIVITY_EXCLUDED_KEYS: frozenset[str] = frozenset({"code", "content", "files"})


def extract_loop_activity_args(action: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return activity-safe action metadata excluding large inline bodies."""
    return {
        k: v
        for k, v in (action or {}).items()
        if k not in _ACTIVITY_EXCLUDED_KEYS
    }


def _file_access_kwargs(action: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "token": action.get("token", ""),
        "approval": action.get("approval", ""),
        "extra_roots": action.get("extra_roots", ""),
    }


def loop_action_kwargs(canonical_type: str, action: Mapping[str, Any]) -> dict[str, Any]:
    """Build exact keyword arguments for a ``_loop_dispatch`` target."""
    if canonical_type == "code":
        return {
            "code": action.get("code", ""),
            "language": action.get("language", "python"),
            "stdin": action.get("stdin", ""),
            "timeout": action.get("timeout", 10),
            "cwd": action.get("cwd") or None,
        }
    if canonical_type == "project":
        return {
            "files_json": action.get("files") or action.get("files_json") or [],
            "commands_json": action.get("commands") or action.get("commands_json") or "",
            "stdin": action.get("stdin", ""),
            "timeout": action.get("timeout", 60),
        }
    if canonical_type == "artifact_generate":
        return {
            "name": action.get("name", "generated-artifact"),
            "brief": action.get("brief", action.get("prompt", "")),
            "kinds": action.get("kinds", "auto"),
            "dimension": action.get("dimension", "auto"),
            "theme": action.get("theme", "auto"),
            "seed": action.get("seed"),
            "output_dir": action.get("output_dir", ""),
        }
    if canonical_type == "artifact_ground":
        return {
            "path": action.get("path", ""),
            "recipe": action.get("recipe", "auto"),
            "requirements_json": action.get("requirements", action.get("requirements_json", "")),
        }
    if canonical_type == "game_reference_suite":
        return {
            "name": action.get("name", "sonder-reference"),
            "theme": action.get("theme", "arcane"),
            "seed": action.get("seed", 1337),
            "max_workers": action.get("max_workers", 2),
            "timeout": action.get("timeout", 30),
        }
    if canonical_type == "game_generate_and_test":
        return {
            "name": action.get("name", "generated-game"),
            "concept": action.get("concept", action.get("prompt", "")),
            "language": action.get("language", "python"),
            "dimension": action.get("dimension", "2d"),
            "theme": action.get("theme", "arcane"),
            "seed": action.get("seed", 1337),
            "tier": action.get("tier", "code"),
            "timeout": action.get("timeout", 30),
            "repair_rounds": action.get("repair_rounds"),
        }
    if canonical_type == "game_generation_campaign":
        return {
            "name": action.get("name", "game-fleet"),
            "concept": action.get("concept", action.get("prompt", "compact action game")),
            "total": action.get("total", 6),
            "language": action.get("language", ""),
            "dimension": action.get("dimension", ""),
            "theme": action.get("theme", "arcane"),
            "tier": action.get("tier", "code"),
            "max_workers": action.get("max_workers", 2),
            "timeout": action.get("timeout", 30),
            "repair_rounds": action.get("repair_rounds"),
        }
    if canonical_type == "offload":
        return {
            "prompt": action.get("prompt", ""),
            "tier": action.get("tier", "fast"),
            "system": action.get("system", ""),
            "temperature": action.get("temperature", 0.2),
            "num_predict": action.get("num_predict", 1024),
            "num_ctx": action.get("num_ctx", 4096),
            "learn": action.get("learn", True),
        }
    if canonical_type == "sonder":
        return {
            "prompt": action.get("prompt", ""),
            "system": action.get("system", ""),
            "temperature": action.get("temperature", 0.2),
            "num_predict": action.get("num_predict", 1024),
            "num_ctx": action.get("num_ctx", 4096),
            "context_size": action.get("context_size", ""),
            "trace": action.get("trace", False),
            "strict": action.get("strict"),
            "persona": action.get("persona", ""),
            "session": action.get("session", ""),
            "project": action.get("project", ""),
            "tier": action.get("tier", ""),
        }
    if canonical_type in ("context_health", "improvement_report"):
        return {
            "session": action.get("session", ""),
            "project": action.get("project", ""),
        }
    if canonical_type == "memory_quality_report":
        return {"sample_limit": action.get("sample_limit", 5)}
    if canonical_type in ("memory_quality_repair", "self_heal_repair"):
        return {"apply": action.get("apply") is True}
    if canonical_type == "memory_privacy_review":
        return {"sample_limit": action.get("sample_limit", 20)}
    if canonical_type == "memory_privacy_repair":
        return {
            "lesson_ids_json": action.get("lesson_ids", action.get("lesson_ids_json", [])),
            "apply": action.get("apply") is True,
        }
    if canonical_type in ("memory_embedding_backfill", "memory_interaction_embedding_backfill"):
        return {
            "limit": action.get("limit", 25),
            "apply": action.get("apply") is True,
        }
    if canonical_type == "master_status":
        return {
            "include_finished": action.get("include_finished", True),
            "limit": action.get("limit", 20),
        }
    if canonical_type == "master_capacity":
        capacity_args: dict[str, Any] = {
            "requested_agents": action.get("requested_agents", action.get("agents", 0)),
        }
        if "worker_cap" in action:
            capacity_args["worker_cap"] = action.get("worker_cap")
        return capacity_args
    if canonical_type == "master_cancel":
        return {"agent_id": action.get("agent_id", action.get("selector", ""))}
    if canonical_type == "master_retry":
        return {
            "agent_id": action.get("agent_id", action.get("selector", "")),
            "tier": action.get("tier", ""),
        }
    if canonical_type == "master_orchestrate":
        return {
            "task": action.get("task", action.get("prompt", "")),
            "mode": action.get("mode", "ask"),
            "agents": action.get("agents", 0),
            "worker_cap": action.get("worker_cap", 0),
            "tier": action.get("tier", "auto"),
            "learn": action.get("learn", False),
            "project": action.get("project", ""),
        }
    if canonical_type == "workbench_agent":
        return {
            "prompt": action.get("task", action.get("prompt", "")),
            "tier": action.get("tier", "auto"),
            "max_steps": action.get("max_steps", 12),
            "allow_web": action.get("allow_web", True),
            "project": action.get("project", ""),
            "allow_location": action.get("allow_location", False),
        }
    if canonical_type == "workspace_inventory":
        return {
            "path": action.get("path", action.get("root", ".")),
            "max_entries": action.get("max_entries", 20000),
            "timeout_seconds": action.get("timeout_seconds", 10.0),
            "top_n": action.get("top_n", 15),
            "include_hidden": action.get("include_hidden", False),
            "include_ignored": action.get("include_ignored", False),
            **_file_access_kwargs(action),
        }
    if canonical_type == "directory_tree":
        return {
            "path": action.get("path", "."),
            "depth": action.get("depth", 2),
            "max_entries": action.get("max_entries", 200),
            "include_hidden": action.get("include_hidden", False),
            **_file_access_kwargs(action),
        }
    if canonical_type == "directory_create":
        return {
            "path": action.get("path", ""),
            "parents": action.get("parents", True),
            **_file_access_kwargs(action),
        }
    if canonical_type == "file_read_range":
        return {
            "path": action.get("path", ""),
            "start_line": action.get("start_line", 1),
            "end_line": action.get("end_line", 200),
            **_file_access_kwargs(action),
        }
    if canonical_type == "text_search":
        return {
            "query": action.get("query", ""),
            "root": action.get("root", "."),
            "glob": action.get("glob", "*"),
            "regex": action.get("regex", False),
            "case_sensitive": action.get("case_sensitive", False),
            "max_results": action.get("max_results", 100),
            "max_entries": action.get("max_entries", 20000),
            "timeout_seconds": action.get("timeout_seconds", 10.0),
            "include_hidden": action.get("include_hidden", False),
            "include_ignored": action.get("include_ignored", False),
            **_file_access_kwargs(action),
        }
    if canonical_type == "script_search":
        return {
            "query": action.get("query", "*"),
            "root": action.get("root", "."),
            "max_results": action.get("max_results", 100),
            "max_entries": action.get("max_entries", 20000),
            "timeout_seconds": action.get("timeout_seconds", 10.0),
            "include_hidden": action.get("include_hidden", False),
            "include_ignored": action.get("include_ignored", False),
            **_file_access_kwargs(action),
        }
    if canonical_type == "program_search":
        return {
            "query": action.get("query", "*"),
            "max_results": action.get("max_results", 100),
        }
    if canonical_type == "workspace_run":
        return {
            "program": action.get("program", ""),
            "args_json": action.get("args", action.get("args_json", [])),
            "cwd": action.get("cwd", "."),
            "stdin": action.get("stdin", ""),
            "timeout": action.get("timeout", 30),
            "max_output": action.get("max_output", 128000),
            **_file_access_kwargs(action),
        }
    if canonical_type == "script_run":
        return {
            "path": action.get("path", ""),
            "args_json": action.get("args", action.get("args_json", [])),
            "cwd": action.get("cwd", ""),
            "stdin": action.get("stdin", ""),
            "timeout": action.get("timeout", 30),
            "max_output": action.get("max_output", 128000),
            "risk_policy": action.get("risk_policy", ""),
            **_file_access_kwargs(action),
        }
    if canonical_type == "artifact_risk_inspect":
        return {
            "path": action.get("path", ""),
            "max_scan_bytes": action.get("max_scan_bytes", 16 * 1024 * 1024),
            "max_seconds": action.get("max_seconds", 5.0),
            **_file_access_kwargs(action),
        }
    if canonical_type == "process_list":
        return {
            "max_processes": action.get("max_processes", 128),
            "max_seconds": action.get("max_seconds", 0.5),
        }
    if canonical_type == "process_memory_risk_inspect":
        return {
            "pid": action.get("pid", 0),
            "max_bytes": action.get("max_bytes", 4 * 1024 * 1024),
            "max_regions": action.get("max_regions", 256),
            "max_seconds": action.get("max_seconds", 1.0),
        }
    if canonical_type == "image_inspect":
        return {
            "path": action.get("path", ""),
            **_file_access_kwargs(action),
        }
    if canonical_type in ("data_inspect", "file_read"):
        return {
            "path": action.get("path", ""),
            "max_bytes": action.get("max_bytes", 256000),
            **_file_access_kwargs(action),
        }
    if canonical_type == "checklist_create":
        items = action.get("items", action.get("items_json", []))
        return {
            "title": action.get("title", "Workflow checklist"),
            "items_json": items if isinstance(items, str) else json.dumps(items),
            "project": action.get("project", ""),
            "owner": action.get("owner", "workflow"),
            "priority": action.get("priority", 1),
        }
    if canonical_type == "checklist_update":
        return {
            "checklist_id": action.get("checklist_id", action.get("id", "")),
            "item": str(action.get("item", "")),
            "status": action.get("status", "in_progress"),
            "note": action.get("note", ""),
        }
    if canonical_type == "checklist_show":
        return {"checklist_id": action.get("checklist_id", action.get("id", ""))}
    if canonical_type == "file_policy":
        return _file_access_kwargs(action)
    if canonical_type == "file_find":
        return {
            "query": action.get("query", "*"),
            "root": action.get("root", ""),
            "max_results": action.get("max_results", 50),
            **_file_access_kwargs(action),
        }
    if canonical_type == "file_write":
        return {
            "path": action.get("path", ""),
            "content": action.get("content", ""),
            "mode": action.get("mode", "create"),
            **_file_access_kwargs(action),
        }
    if canonical_type in ("file_copy", "file_move"):
        return {
            "source": action.get("source", ""),
            "destination": action.get("destination", ""),
            "overwrite": action.get("overwrite", False),
            **_file_access_kwargs(action),
        }
    if canonical_type == "file_edit":
        return {
            "path": action.get("path", ""),
            "old": action.get("old", ""),
            "new": action.get("new", ""),
            "count": action.get("count", 1),
            **_file_access_kwargs(action),
        }
    if canonical_type == "file_delete":
        return {
            "path": action.get("path", ""),
            "recursive": action.get("recursive") is True,
            "dry_run": action.get("dry_run") is not False,
            "confirm": action.get("confirm", ""),
            **_file_access_kwargs(action),
        }
    if canonical_type == "emotion_update":
        payload = action.get("vectors", action.get("vectors_json", {}))
        return {
            "vectors_json": payload if isinstance(payload, str) else json.dumps(payload),
            "mode": action.get("mode", "merge"),
        }
    if canonical_type == "emotion_tune":
        return {
            "feedback_text": action.get("feedback_text", action.get("text", "")),
            "step": action.get("step", 0.1),
        }
    if canonical_type == "learn_preference":
        return {
            "text": action.get("text", ""),
            "scope": action.get("scope", "global"),
        }
    if canonical_type == "preferences_status":
        return {
            "include_disabled": action.get("include_disabled", False),
            "limit": action.get("limit", 50),
        }
    if canonical_type == "memory_search":
        return {
            "query": action.get("query", ""),
            "limit": action.get("limit", 10),
        }
    if canonical_type == "ground_artifact":
        return {
            "artifact": action.get("artifact", ""),
            "checks_json": json.dumps(action.get("checks", [])),
        }
    if canonical_type == "apply_learned":
        return {
            "task": action.get("task", ""),
            "limit": action.get("limit", 5),
        }
    if canonical_type == "web_search":
        return {
            "query": action.get("query", ""),
            "limit": action.get("limit", 5),
        }
    if canonical_type == "web_fetch":
        return {
            "url": action.get("url", ""),
            "max_chars": action.get("max_chars", 8000),
        }
    if canonical_type == "weather_lookup":
        return {
            "location": action.get("location", ""),
            "forecast_days": action.get("forecast_days", 3),
            "units": action.get("units", "auto"),
        }
    if canonical_type == "approximate_location_lookup":
        return {"consent": action.get("consent") is True}
    raise KeyError(f"unsupported loop action type: {canonical_type}")


def format_code_loop_result(
    result: Mapping[str, Any],
    formatted_output: str,
) -> dict[str, Any]:
    """Assemble the loop dispatch payload for a ``code`` action."""
    rc = result.get("returncode")
    summary = result.get("error") or "returncode %s" % (
        "(none)" if rc is None else rc
    )
    return {
        "ok": result.get("ok"),
        "type": "code",
        "summary": summary,
        "output": formatted_output,
    }


def format_project_loop_result(
    result: Mapping[str, Any],
    formatted_output: str,
) -> dict[str, Any]:
    """Assemble the loop dispatch payload for a ``project`` action."""
    return {
        "ok": result.get("ok"),
        "type": "project",
        "summary": "project %s" % ("ok" if result.get("ok") else "failed"),
        "output": formatted_output,
    }


def format_project_loop_error(message: str) -> dict[str, Any]:
    """Assemble the loop dispatch payload when ``run_project`` rejects inputs."""
    return {
        "ok": False,
        "type": "project",
        "summary": message,
        "output": "",
    }


def format_sleep_loop_result(seconds: float) -> dict[str, Any]:
    """Assemble the loop dispatch payload for a ``sleep`` action."""
    return {
        "ok": True,
        "type": "sleep",
        "summary": "slept for %.2fs" % seconds,
        "output": "",
    }


def format_unknown_loop_result(
    action_type: str,
    valid_types: Sequence[str],
) -> dict[str, Any]:
    """Assemble the loop dispatch payload for an unrecognized action type."""
    return {
        "ok": False,
        "type": action_type or "(unknown)",
        "summary": "unknown action type",
        "output": "Valid action types: %s." % ", ".join(valid_types),
    }
