"""Pure argument unpacking and project-bound helpers for ``_agent_dispatch``."""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


def _file_access_kwargs(args: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "token": args.get("token", ""),
        "approval": args.get("approval", ""),
        "extra_roots": args.get("extra_roots", ""),
    }


def _json_or_str(value: Any, *, ensure_ascii: bool = True) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=ensure_ascii)


def resolve_bound_master_project(
    requested_project: str,
    repository_extra_roots: str,
    *,
    reject_network_path: Callable[[str], str],
    is_foreign_absolute: Callable[[str], bool],
) -> tuple[str, str]:
    """Confine a ``master_orchestrate`` project path under ``repository_extra_roots``.

    Returns ``(resolved_project, rejection_detail)``; empty detail means admitted.
    """
    if not requested_project:
        return repository_extra_roots, ""
    try:
        raw_root = reject_network_path(str(repository_extra_roots).strip())
        bound_root = Path(raw_root).expanduser().resolve(strict=True)
        if not bound_root.is_dir():
            raise ValueError("project root is not a directory")
        raw_req = reject_network_path(requested_project)
        if is_foreign_absolute(raw_req):
            raise PermissionError(
                "drive-relative or foreign absolute project path"
            )
        req_path = Path(raw_req).expanduser()
        if not req_path.is_absolute():
            req_path = bound_root / req_path
        req_resolved = req_path.resolve(strict=False)
        req_resolved.relative_to(bound_root)
        return str(req_resolved), ""
    except (OSError, PermissionError, ValueError) as exc:
        return (
            "",
            "agent project path rejected: master_orchestrate "
            "project is outside the host-selected project root (%s)" % exc,
        )


def agent_tool_kwargs(
    tool_name: str,
    args: Mapping[str, Any],
    *,
    allow_web: bool = True,
    read_only: bool = False,
    repository_extra_roots: str = "",
    archive_defaults: Mapping[str, Any] | None = None,
    archive_create_defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build exact keyword arguments for an explicit ``_agent_dispatch`` branch."""
    if tool_name == "run_code":
        return {
            "code": args.get("code", ""),
            "language": args.get("language", "python"),
            "stdin": args.get("stdin", ""),
            "timeout": args.get("timeout", 10),
        }
    if tool_name == "toolchain_status":
        return {
            "name": args.get("name", ""),
            "refresh": bool(args.get("refresh", False)),
        }
    if tool_name == "tool_inventory":
        return {
            "category": args.get("category", ""),
            "name": args.get("name", ""),
            "refresh": bool(args.get("refresh", False)),
        }
    if tool_name == "output_digest":
        return {
            "path": args.get("path", ""),
            "job_id": args.get("job_id", ""),
            "tail_lines": args.get("tail_lines", 20),
        }
    if tool_name == "run_project":
        return {
            "files_json": args.get("files_json", args.get("files", [])),
            "commands_json": args.get("commands_json", args.get("commands", "")),
            "stdin": args.get("stdin", ""),
            "timeout": args.get("timeout", 60),
        }
    if tool_name == "artifact_generate":
        return {
            "name": args.get("name", "generated-artifact"),
            "brief": args.get("brief", args.get("prompt", "")),
            "kinds": args.get("kinds", "auto"),
            "dimension": args.get("dimension", "auto"),
            "theme": args.get("theme", "auto"),
            "seed": args.get("seed"),
            "output_dir": args.get("output_dir", ""),
        }
    if tool_name == "artifact_ground":
        return {
            "path": args.get("path", ""),
            "recipe": args.get("recipe", "auto"),
            "requirements_json": args.get("requirements_json", args.get("requirements", "")),
            **_file_access_kwargs(args),
        }
    if tool_name == "game_reference_suite":
        return {
            "name": args.get("name", "sonder-reference"),
            "theme": args.get("theme", "arcane"),
            "seed": args.get("seed", 1337),
            "max_workers": args.get("max_workers", 2),
            "timeout": args.get("timeout", 30),
        }
    if tool_name == "game_generate_and_test":
        return {
            "name": args.get("name", "generated-game"),
            "concept": args.get("concept", args.get("prompt", "")),
            "language": args.get("language", "python"),
            "dimension": args.get("dimension", "2d"),
            "theme": args.get("theme", "arcane"),
            "seed": args.get("seed", 1337),
            "tier": args.get("tier", "code"),
            "timeout": args.get("timeout", 30),
            "repair_rounds": args.get("repair_rounds"),
        }
    if tool_name == "game_generation_campaign":
        return {
            "name": args.get("name", "game-fleet"),
            "concept": args.get("concept", args.get("prompt", "compact action game")),
            "total": args.get("total", 6),
            "language": args.get("language", ""),
            "dimension": args.get("dimension", ""),
            "theme": args.get("theme", "arcane"),
            "tier": args.get("tier", "code"),
            "max_workers": args.get("max_workers", 2),
            "timeout": args.get("timeout", 30),
            "repair_rounds": args.get("repair_rounds"),
        }
    if tool_name == "file_policy":
        return _file_access_kwargs(args)
    if tool_name == "repo_status":
        return {
            "root": args.get("root", "."),
            "timeout": args.get("timeout", 10),
            "max_output": args.get("max_output", 128000),
            **_file_access_kwargs(args),
        }
    if tool_name == "repo_diff":
        return {
            "root": args.get("root", "."),
            "staged": args.get("staged") is True,
            "path": args.get("path", ""),
            "context": args.get("context", 3),
            "timeout": args.get("timeout", 10),
            "max_output": args.get("max_output", 128000),
            **_file_access_kwargs(args),
        }
    if tool_name == "workspace_inventory":
        return {
            "path": args.get("path", args.get("root", ".")),
            "max_entries": args.get("max_entries", 20000),
            "timeout_seconds": args.get("timeout_seconds", 10.0),
            "top_n": args.get("top_n", 15),
            "include_hidden": args.get("include_hidden", False),
            "include_ignored": args.get("include_ignored", False),
            **_file_access_kwargs(args),
        }
    if tool_name == "project_detect":
        return {
            "path": args.get("path", args.get("root", ".")),
            "max_depth": args.get("max_depth", 8),
            "max_files": args.get("max_files", 200),
            "max_total_bytes": args.get("max_total_bytes", 2_000_000),
            "max_file_bytes": args.get("max_file_bytes", 256_000),
            "max_results": args.get("max_results", 500),
            **_file_access_kwargs(args),
        }
    if tool_name == "dependency_inventory":
        return {
            "path": args.get("path", args.get("root", ".")),
            "max_depth": args.get("max_depth", 5),
            "max_files": args.get("max_files", 100),
            "max_total_bytes": args.get("max_total_bytes", 2000000),
            "max_results": args.get("max_results", 2000),
            **_file_access_kwargs(args),
        }
    if tool_name == "directory_tree":
        return {
            "path": args.get("path", args.get("root", ".")),
            "depth": args.get("depth", 2),
            "max_entries": args.get("max_entries", 200),
            "include_hidden": args.get("include_hidden", False),
            "include_ignored": args.get("include_ignored", False),
            **_file_access_kwargs(args),
        }
    if tool_name == "directory_create":
        return {
            "path": args.get("path", ""),
            "parents": args.get("parents", True),
            **_file_access_kwargs(args),
        }
    if tool_name == "file_find":
        return {
            "query": args.get("query", "*"),
            "root": args.get("root", ""),
            "max_results": args.get("max_results", 50),
            "include_ignored": args.get("include_ignored", False),
            **_file_access_kwargs(args),
        }
    if tool_name == "repository_symbol_index":
        return {
            "path": args.get("path", args.get("root", ".")),
            "glob": args.get("glob", "*"),
            "language": args.get("language", ""),
            "max_files": args.get("max_files", 200),
            "max_total_bytes": args.get("max_total_bytes", 2_000_000),
            "max_file_bytes": args.get("max_file_bytes", 256_000),
            "max_symbols": args.get("max_symbols", 2_000),
            **_file_access_kwargs(args),
        }
    if tool_name == "file_read_range":
        return {
            "path": args.get("path", ""),
            "start_line": args.get("start_line", args.get("start", 1)),
            "end_line": args.get("end_line", args.get("end", 200)),
            **_file_access_kwargs(args),
        }
    if tool_name == "context_pack":
        return {
            "paths_json": args.get("paths_json", args.get("paths", [])),
            "max_files": args.get("max_files", 12),
            "max_total_bytes": args.get("max_total_bytes", 256000),
            "max_bytes_per_file": args.get("max_bytes_per_file", 64000),
            **_file_access_kwargs(args),
        }
    if tool_name == "data_inspect":
        return {
            "path": args.get("path", ""),
            "max_bytes": args.get("max_bytes", 256000),
            **_file_access_kwargs(args),
        }
    if tool_name == "data_query":
        return {
            "path": args.get("path", ""),
            "sql": args.get("sql", ""),
            "projection_json": (
                json.dumps(args.get("projection_json"))
                if not isinstance(args.get("projection_json", "[]"), str)
                else args.get("projection_json", "[]")
            ),
            "filters_json": (
                json.dumps(args.get("filters_json"))
                if not isinstance(args.get("filters_json", "{}"), str)
                else args.get("filters_json", "{}")
            ),
            "max_rows": args.get("max_rows", 100),
            "max_columns": args.get("max_columns", 50),
            "max_output_bytes": args.get("max_output_bytes", 256000),
            "max_scan_bytes": args.get("max_scan_bytes", 4000000),
            "timeout": args.get("timeout", 5.0),
            **_file_access_kwargs(args),
        }
    if tool_name == "data_convert":
        fields = _json_or_str(
            args.get("fields_json", args.get("fields", [])), ensure_ascii=False,
        )
        return {
            "input_path": args.get("input_path", ""),
            "output_path": args.get("output_path", ""),
            "fields_json": fields,
            "output_format": args.get("output_format", ""),
            "apply": args.get("apply", False),
            "max_input_bytes": args.get("max_input_bytes", 16_000_000),
            "max_output_bytes": args.get("max_output_bytes", 16_000_000),
            "max_rows": args.get("max_rows", 10_000),
            "max_columns": args.get("max_columns", 100),
            "max_fields": args.get("max_fields", 50),
            "max_field_bytes": args.get("max_field_bytes", 64_000),
            "max_depth": args.get("max_depth", 16),
            "preview_rows": args.get("preview_rows", 5),
            "timeout": args.get("timeout", 10.0),
            **_file_access_kwargs(args),
        }
    if tool_name == "sqlite_mutate":
        parameters = _json_or_str(
            args.get("parameters_json", args.get("parameters", [])),
            ensure_ascii=False,
        )
        return {
            "path": args.get("path", ""),
            "sql": args.get("sql", ""),
            "parameters_json": parameters,
            "mode": args.get("mode", "preview"),
            "max_rows": args.get("max_rows", 1000),
            "timeout": args.get("timeout", 2.0),
            "max_db_bytes": args.get("max_db_bytes", 67108864),
            "preview_token": args.get("preview_token", ""),
            **_file_access_kwargs(args),
        }
    if tool_name == "log_inspect":
        return {
            "path": args.get("path", ""),
            "tail_lines": args.get("tail_lines", 0),
            "context_lines": args.get("context_lines", 2),
            "max_file_bytes": args.get("max_file_bytes", 64000000),
            "max_scan_bytes": args.get("max_scan_bytes", 4000000),
            "max_lines": args.get("max_lines", 10000),
            "max_line_bytes": args.get("max_line_bytes", 4096),
            "max_results": args.get("max_results", 100),
            "max_output_bytes": args.get("max_output_bytes", 256000),
            "timeout": args.get("timeout", 5.0),
            **_file_access_kwargs(args),
        }
    if tool_name == "workspace_compare":
        return {
            "left": args.get("left", ""),
            "right": args.get("right", ""),
            "max_entries": args.get("max_entries", 2000),
            "max_file_bytes": args.get("max_file_bytes", 64000000),
            "max_total_bytes": args.get("max_total_bytes", 256000000),
            "max_details": args.get("max_details", 1000),
            "max_output_bytes": args.get("max_output_bytes", 256000),
            "timeout": args.get("timeout", 5.0),
            **_file_access_kwargs(args),
        }
    if tool_name == "repo_log":
        return {
            "path": args.get("path", "."),
            "revision": args.get("revision", "HEAD"),
            "file_path": args.get("file_path", ""),
            "count": args.get("count", 20),
            "timeout": args.get("timeout", 5.0),
            "max_bytes": args.get("max_bytes", 256000),
            **_file_access_kwargs(args),
        }
    if tool_name == "repo_show":
        return {
            "path": args.get("path", "."),
            "revision": args.get("revision", "HEAD"),
            "file_path": args.get("file_path", ""),
            "timeout": args.get("timeout", 5.0),
            "max_bytes": args.get("max_bytes", 256000),
            **_file_access_kwargs(args),
        }
    if tool_name == "repo_blame":
        return {
            "path": args.get("path", "."),
            "file_path": args.get("file_path", ""),
            "revision": args.get("revision", "HEAD"),
            "start_line": args.get("start_line", 1),
            "end_line": args.get("end_line", 0),
            "timeout": args.get("timeout", 5.0),
            "max_bytes": args.get("max_bytes", 256000),
            **_file_access_kwargs(args),
        }
    if tool_name == "archive_list":
        ad = archive_defaults or {}
        return {
            "path": args.get("path", ""),
            "max_entries": args.get("max_entries", ad["max_entries"]),
            "max_file_bytes": args.get("max_file_bytes", ad["max_file_bytes"]),
            "max_total_bytes": args.get("max_total_bytes", ad["max_total_bytes"]),
            "max_ratio": args.get("max_ratio", ad["max_ratio"]),
            "max_path_depth": args.get("max_path_depth", ad["max_path_depth"]),
            "max_results": args.get("max_results", ad["max_results"]),
            "max_seconds": args.get("max_seconds", ad["max_seconds"]),
            **_file_access_kwargs(args),
        }
    if tool_name == "archive_extract":
        ad = archive_defaults or {}
        return {
            "source": args.get("source", ""),
            "destination": args.get("destination", ""),
            "max_entries": args.get("max_entries", ad["max_entries"]),
            "max_file_bytes": args.get("max_file_bytes", ad["max_file_bytes"]),
            "max_total_bytes": args.get("max_total_bytes", ad["max_total_bytes"]),
            "max_ratio": args.get("max_ratio", ad["max_ratio"]),
            "max_path_depth": args.get("max_path_depth", ad["max_path_depth"]),
            "max_seconds": args.get("max_seconds", ad["max_seconds"]),
            **_file_access_kwargs(args),
        }
    if tool_name == "archive_create":
        acd = archive_create_defaults or {}
        inputs = _json_or_str(
            args.get("inputs_json", args.get("inputs", [])), ensure_ascii=False,
        )
        return {
            "root": args.get("root", "."),
            "inputs_json": inputs,
            "destination": args.get("destination", ""),
            "archive_format": args.get("archive_format", args.get("format", "zip")),
            "deterministic": args.get("deterministic", True),
            "max_files": args.get("max_files", acd["max_files"]),
            "max_entries": args.get("max_entries", acd["max_entries"]),
            "max_file_bytes": args.get("max_file_bytes", acd["max_file_bytes"]),
            "max_total_bytes": args.get("max_total_bytes", acd["max_total_bytes"]),
            "max_depth": args.get("max_depth", acd["max_depth"]),
            "max_results": args.get("max_results", acd["max_results"]),
            **_file_access_kwargs(args),
        }
    if tool_name == "text_search":
        return {
            "query": args.get("query", args.get("pattern", "")),
            "root": args.get("root", "."),
            "glob": args.get("glob", "*"),
            "regex": args.get("regex", False),
            "case_sensitive": args.get("case_sensitive", False),
            "max_results": args.get("max_results", 100),
            "max_entries": args.get("max_entries", 20000),
            "timeout_seconds": args.get("timeout_seconds", 10.0),
            "include_hidden": args.get("include_hidden", False),
            "include_ignored": args.get("include_ignored", False),
            **_file_access_kwargs(args),
        }
    if tool_name == "file_digest":
        return {
            "path": args.get("path", ""),
            "max_bytes": args.get("max_bytes", 32_000_000),
            **_file_access_kwargs(args),
        }
    if tool_name == "directory_digest":
        return {
            "path": args.get("path", args.get("root", ".")),
            "max_depth": args.get("max_depth", 12),
            "max_files": args.get("max_files", 2_000),
            "max_total_bytes": args.get("max_total_bytes", 32_000_000),
            "max_file_bytes": args.get("max_file_bytes", 32_000_000),
            "max_results": args.get("max_results", 2_500),
            **_file_access_kwargs(args),
        }
    if tool_name == "file_write":
        return {
            "path": args.get("path", ""),
            "content": args.get("content", ""),
            "mode": args.get("mode", "create"),
            **_file_access_kwargs(args),
        }
    if tool_name in ("file_copy", "file_move"):
        return {
            "source": args.get("source", ""),
            "destination": args.get("destination", ""),
            "overwrite": args.get("overwrite", False),
            **_file_access_kwargs(args),
        }
    if tool_name == "file_batch_write":
        operations = _json_or_str(
            args.get("operations_json", args.get("operations", [])),
            ensure_ascii=False,
        )
        return {
            "operations_json": operations,
            **_file_access_kwargs(args),
        }
    if tool_name == "json_patch":
        operations = _json_or_str(
            args.get("operations_json", args.get("operations", [])),
            ensure_ascii=False,
        )
        return {
            "path": args.get("path", ""),
            "operations_json": operations,
            "mode": args.get("mode", "preview"),
            **_file_access_kwargs(args),
        }
    if tool_name == "text_patch":
        return {
            "root": args.get("root", "."),
            "patch": args.get("patch", ""),
            "apply": args.get("apply") is True,
            **_file_access_kwargs(args),
        }
    if tool_name == "scaffold_project":
        return {
            "kind": args.get("kind", ""),
            "name": args.get("name", ""),
            "root": args.get("root", ""),
            "apply": bool(args.get("apply", True)),
        }
    if tool_name == "environment_status":
        return {"refresh": bool(args.get("refresh", False))}
    if tool_name == "hardware_profile":
        return {
            "workload": args.get("workload", "general"),
            "refresh": bool(args.get("refresh", False)),
            "model": args.get("model", ""),
        }
    if tool_name == "file_edit":
        return {
            "path": args.get("path", ""),
            "old": args.get("old", ""),
            "new": args.get("new", ""),
            "count": args.get("count", 1),
            **_file_access_kwargs(args),
        }
    if tool_name == "file_delete":
        return {
            "path": args.get("path", ""),
            "recursive": args.get("recursive", False),
            "dry_run": args.get("dry_run", True),
            "confirm": args.get("confirm", ""),
            **_file_access_kwargs(args),
        }
    if tool_name == "script_search":
        return {
            "query": args.get("query", "*"),
            "root": args.get("root", "."),
            "max_results": args.get("max_results", 100),
            "max_entries": args.get("max_entries", 20000),
            "timeout_seconds": args.get("timeout_seconds", 10.0),
            "include_hidden": args.get("include_hidden", False),
            "include_ignored": args.get("include_ignored", False),
            **_file_access_kwargs(args),
        }
    if tool_name == "program_search":
        return {
            "query": args.get("query", "*"),
            "max_results": args.get("max_results", 100),
        }
    if tool_name == "workspace_run":
        return {
            "program": args.get("program", ""),
            "args_json": args.get("args_json", args.get("args", [])),
            "cwd": args.get("cwd", "."),
            "stdin": args.get("stdin", ""),
            "timeout": args.get("timeout", 30),
            "max_output": args.get("max_output", 128000),
            **_file_access_kwargs(args),
        }
    if tool_name == "script_run":
        return {
            "path": args.get("path", ""),
            "args_json": args.get("args_json", args.get("args", [])),
            "cwd": args.get("cwd", ""),
            "stdin": args.get("stdin", ""),
            "timeout": args.get("timeout", 30),
            "max_output": args.get("max_output", 128000),
            "risk_policy": args.get("risk_policy", ""),
            **_file_access_kwargs(args),
        }
    if tool_name == "artifact_risk_inspect":
        return {
            "path": args.get("path", ""),
            "max_scan_bytes": args.get("max_scan_bytes", 16 * 1024 * 1024),
            "max_seconds": args.get("max_seconds", 5.0),
            **_file_access_kwargs(args),
        }
    if tool_name == "verify_artifact":
        return {
            "path": args.get("path", ""),
            "expect_type": args.get("expect_type", ""),
            "expect_publisher": args.get("expect_publisher", ""),
            "sha256": args.get("sha256", ""),
            **_file_access_kwargs(args),
        }
    if tool_name == "process_list":
        return {
            "max_processes": args.get("max_processes", 128),
            "max_seconds": args.get("max_seconds", 0.5),
        }
    if tool_name == "process_memory_risk_inspect":
        return {
            "pid": args.get("pid", 0),
            "max_bytes": args.get("max_bytes", 4 * 1024 * 1024),
            "max_regions": args.get("max_regions", 256),
            "max_seconds": args.get("max_seconds", 1.0),
        }
    if tool_name in ("image_inspect", "media_inspect"):
        return {
            "path": args.get("path", ""),
            **_file_access_kwargs(args),
        }
    if tool_name == "document_extract":
        return {
            "path": args.get("path", ""),
            "max_chars": args.get("max_chars", 32000),
            "max_items": args.get("max_items", 200),
            **_file_access_kwargs(args),
        }
    if tool_name == "code_metrics":
        return {
            "path": args.get("path", "."),
            "max_files": args.get("max_files", 2000),
            "top_n": args.get("top_n", 10),
            **_file_access_kwargs(args),
        }
    if tool_name == "schema_validate":
        return {
            "path": args.get("path", ""),
            "schema_path": args.get("schema_path", ""),
            "schema_json": args.get("schema_json", ""),
            **_file_access_kwargs(args),
        }
    if tool_name == "language_matrix":
        return {"language": args.get("language", "")}
    if tool_name == "math_eval":
        return {
            "expression": args.get("expression", ""),
            "action": args.get("action", "eval"),
            "exact": bool(args.get("exact", False)),
            "precision": args.get("precision", 0),
            "variables_json": args.get("variables_json", args.get("variables", "")),
            "variable": args.get("variable", "x"),
            "equations_json": args.get("equations_json", args.get("equations", "")),
            "bracket_json": args.get("bracket_json", args.get("bracket", "")),
            "matrix_op": args.get("matrix_op", ""),
            "matrix_a_json": args.get("matrix_a_json", args.get("matrix_a", "")),
            "matrix_b_json": args.get("matrix_b_json", args.get("matrix_b", "")),
            "value": args.get("value"),
            "from_unit": args.get("from_unit", ""),
            "to_unit": args.get("to_unit", ""),
            "date_a": args.get("date_a", ""),
            "date_b": args.get("date_b", ""),
            "add_days": args.get("add_days"),
            "at": args.get("at", 0.0),
            "low": args.get("low", 0.0),
            "high": args.get("high", 1.0),
        }
    if tool_name == "logic_verify":
        return {
            "formula": args.get("formula", ""),
            "action": args.get("action", "satisfiable"),
            "formula_b": args.get("formula_b", ""),
            "premises_json": args.get("premises_json", args.get("premises", "")),
            "conclusion": args.get("conclusion", ""),
            "domains_json": args.get("domains_json", args.get("domains", "")),
            "constraints_json": args.get("constraints_json", args.get("constraints", "")),
        }
    if tool_name == "regex_lab":
        return {
            "pattern": args.get("pattern", ""),
            "text": args.get("text", ""),
            "samples_json": args.get("samples_json", args.get("samples", "")),
            "should_match_json": args.get("should_match_json", args.get("should_match", "")),
            "should_not_match_json": args.get(
                "should_not_match_json", args.get("should_not_match", ""),
            ),
            "replacement": args.get("replacement"),
            "flags": args.get("flags", ""),
            "mode": args.get("mode", "search"),
            "max_matches": args.get("max_matches", 20),
        }
    if tool_name == "code_graph":
        return {
            "root": args.get("root", "."),
            "action": args.get("action", "summary"),
            "target": args.get("target", ""),
            "max_files": args.get("max_files", 1500),
            "top_n": args.get("top_n", 15),
            "max_depth": args.get("max_depth", 6),
            **_file_access_kwargs(args),
        }
    if tool_name == "diff_semantic":
        return {
            "path_a": args.get("path_a", ""),
            "path_b": args.get("path_b", ""),
            "text_a": args.get("text_a", ""),
            "text_b": args.get("text_b", ""),
            "kind": args.get("kind", "auto"),
            **_file_access_kwargs(args),
        }
    if tool_name == "reasoning_board":
        return {
            "action": args.get("action", "summary"),
            "board_id": args.get("board_id", "default"),
            "goal": args.get("goal", ""),
            "text": args.get("text", ""),
            "source": args.get("source", ""),
            "hypothesis_id": args.get("hypothesis_id", ""),
            "status": args.get("status", ""),
            "confidence": args.get("confidence"),
            "evidence_for": args.get("evidence_for", ""),
            "evidence_against": args.get("evidence_against", ""),
            "fact_refs_json": args.get("fact_refs_json", args.get("fact_refs", "")),
            "alternatives_json": args.get("alternatives_json", args.get("alternatives", "")),
        }
    if tool_name == "scratchpad_read":
        return {
            "name": args.get("name", ""),
            "query": args.get("query", ""),
            "tag": args.get("tag", ""),
            "start_line": args.get("start_line", 1),
            "max_lines": args.get("max_lines", 200),
        }
    if tool_name == "scratchpad_write":
        return {
            "name": args.get("name", ""),
            "content": args.get("content", ""),
            "mode": args.get("mode", "replace"),
            "tags": args.get("tags"),
        }
    if tool_name == "scratchpad_delete":
        return {"name": args.get("name", "")}
    if tool_name == "symbol_verify":
        return {
            "action": args.get("action", "verify_symbol"),
            "module": args.get("module", ""),
            "symbol": args.get("symbol", ""),
            "kwargs_json": args.get("kwargs_json", args.get("kwargs", "")),
            "code": args.get("code", ""),
            "root": args.get("root", ""),
            **_file_access_kwargs(args),
        }
    if tool_name == "citation_audit":
        return {
            "text": args.get("text", ""),
            "root": args.get("root", ""),
            "claims_json": args.get("claims_json", args.get("claims", "")),
            "sources_json": args.get("sources_json", args.get("sources", "")),
            **_file_access_kwargs(args),
        }
    if tool_name == "fact_cross_check":
        return {"text": args.get("text", "")}
    if tool_name == "consistency_check":
        return {
            "action": args.get("action", "compare_candidates"),
            "candidates_json": args.get("candidates_json", args.get("candidates", "")),
            "reply": args.get("reply", ""),
            "receipts_json": args.get("receipts_json", args.get("receipts", "")),
        }
    if tool_name == "plan_fallback":
        return {
            "failed_tool": args.get("failed_tool", ""),
            "error": args.get("error", ""),
            "goal": args.get("goal", ""),
            "available_tools_json": args.get(
                "available_tools_json", args.get("available_tools", ""),
            ),
            "tried_tools_json": args.get("tried_tools_json", args.get("tried_tools", "")),
        }
    if tool_name == "prompt_shield":
        return {
            "text": args.get("text", ""),
            "path": args.get("path", ""),
            "redact_secrets": bool(args.get("redact_secrets", True)),
            "include_sanitized": bool(args.get("include_sanitized", True)),
            "max_findings": int(args.get("max_findings", 40)),
        }
    if tool_name == "error_diagnose":
        return {
            "error_text": args.get("error_text", ""),
            "root": args.get("root", ""),
            "max_frames": int(args.get("max_frames", 8)),
            "context_lines": int(args.get("context_lines", 3)),
        }
    if tool_name == "code_audit":
        return {
            "code": args.get("code", ""),
            "path": args.get("path", ""),
            "language": args.get("language", ""),
            "min_severity": args.get("min_severity", "low"),
            "max_findings": int(args.get("max_findings", 60)),
        }
    if tool_name == "data_transform":
        return {
            "action": args.get("action", "convert"),
            "data": args.get("data", ""),
            "path": args.get("path", ""),
            "input_format": args.get("input_format", "auto"),
            "output_format": args.get("output_format", "json"),
            "expression": args.get("expression", ""),
            "encoding": args.get("encoding", "base64"),
            "limit": int(args.get("limit", 50)),
        }
    if tool_name == "learn_tool":
        return {
            "tool": args.get("tool", ""),
            "goal": args.get("goal", ""),
            "subcommand": args.get("subcommand", ""),
            "probe_cli": bool(args.get("probe_cli", True)),
            "research_web": bool(args.get("research_web", False)) and bool(allow_web),
            "save_note": bool(args.get("save_note", False)) and not bool(read_only),
            "timeout": float(args.get("timeout", 6.0)),
        }
    if tool_name == "task_create":
        return {
            "title": args.get("title", ""),
            "detail": args.get("detail", ""),
            "priority": args.get("priority", 2),
            "project": args.get("project", ""),
            "owner": args.get("owner", ""),
            "parent_id": args.get("parent_id", ""),
        }
    if tool_name == "task_list":
        return {
            "status": args.get("status", ""),
            "project": args.get("project", ""),
            "owner": args.get("owner", ""),
            "include_done": args.get("include_done", False),
            "limit": args.get("limit", 50),
        }
    if tool_name == "task_update":
        return {
            "task_id": args.get("task_id", args.get("id", "")),
            "status": args.get("status", ""),
            "title": args.get("title", ""),
            "detail": args.get("detail", ""),
            "priority": args.get("priority", ""),
            "project": args.get("project", ""),
            "owner": args.get("owner", ""),
            "note": args.get("note", ""),
        }
    if tool_name == "task_plan":
        steps = args.get("steps", args.get("items", []))
        return {
            "title": args.get("title", "Work plan"),
            "steps": _json_or_str(steps),
            "project": args.get("project", ""),
            "owner": args.get("owner", "agent"),
            "priority": args.get("priority", 2),
            "sequential": args.get("sequential", True),
        }
    if tool_name == "task_progress":
        return {"project": args.get("project", "")}
    if tool_name == "task_ledger":
        return {
            "goal_id": args.get("goal_id", args.get("id", "")),
            "replan_count": args.get("replan_count", 0),
            "last_replan_reason": args.get("last_replan_reason", ""),
        }
    if tool_name == "task_depend":
        return {
            "task_id": args.get("task_id", args.get("id", "")),
            "depends_on": args.get("depends_on", ""),
            "remove": args.get("remove", False),
        }
    if tool_name == "checklist_create":
        items = args.get("items_json", args.get("items", []))
        return {
            "title": args.get("title", "Work checklist"),
            "items_json": _json_or_str(items),
            "project": args.get("project", ""),
            "owner": args.get("owner", "agent"),
            "priority": args.get("priority", 1),
        }
    if tool_name == "checklist_update":
        return {
            "checklist_id": args.get("checklist_id", args.get("id", "")),
            "item": str(args.get("item", args.get("item_id", ""))),
            "status": args.get("status", ""),
            "note": args.get("note", ""),
        }
    if tool_name == "checklist_show":
        return {"checklist_id": args.get("checklist_id", args.get("id", ""))}
    if tool_name in ("context_compaction_plan", "context_health", "system_improvement_report"):
        return {
            "session": args.get("session", ""),
            "project": args.get("project", ""),
        }
    if tool_name == "evaluation_history_status":
        return {
            "model": args.get("model", ""),
            "model_digest": args.get("model_digest", ""),
            "suite": args.get("suite", ""),
            "suite_version": args.get("suite_version", ""),
            "suite_digest": args.get("suite_digest", ""),
            "tolerance": args.get("tolerance", 0.0),
            "max_records": args.get("max_records", 10000),
        }
    if tool_name == "memory_quality_report":
        return {"sample_limit": args.get("sample_limit", 5)}
    if tool_name in ("memory_quality_repair", "self_heal_repair"):
        return {"apply": args.get("apply") is True}
    if tool_name == "memory_privacy_review":
        return {"sample_limit": args.get("sample_limit", 20)}
    if tool_name == "memory_privacy_repair":
        return {
            "lesson_ids_json": args.get("lesson_ids_json", args.get("lesson_ids", [])),
            "apply": args.get("apply") is True,
        }
    if tool_name in ("memory_embedding_backfill", "memory_interaction_embedding_backfill"):
        return {
            "limit": args.get("limit", 25),
            "apply": args.get("apply") is True,
        }
    if tool_name == "master_status":
        return {
            "include_finished": args.get("include_finished", True),
            "limit": args.get("limit", 20),
        }
    if tool_name == "master_capacity":
        capacity_args: dict[str, Any] = {
            "requested_agents": args.get("requested_agents", args.get("agents", 0)),
        }
        if "worker_cap" in args:
            capacity_args["worker_cap"] = args.get("worker_cap")
        return capacity_args
    if tool_name == "master_cancel":
        return {"agent_id": args.get("agent_id", args.get("selector", ""))}
    if tool_name == "master_retry":
        return {
            "agent_id": args.get("agent_id", args.get("selector", "")),
            "tier": args.get("tier", ""),
        }
    if tool_name == "tune_emotion_vectors":
        return {
            "feedback_text": args.get("feedback_text", args.get("text", "")),
            "step": args.get("step", 0.1),
        }
    if tool_name == "learn_preference":
        return {
            "text": args.get("text", ""),
            "scope": args.get("scope", "global"),
        }
    if tool_name == "preferences_status":
        return {
            "include_disabled": args.get("include_disabled", False),
            "limit": args.get("limit", 50),
        }
    if tool_name == "offload":
        return {
            "prompt": args.get("prompt", ""),
            "tier": args.get("tier", "fast"),
            "system": args.get("system", ""),
            "temperature": args.get("temperature", 0.2),
            "num_predict": args.get("num_predict", 1024),
            "num_ctx": args.get("num_ctx", 4096),
            "learn": args.get("learn", False),
        }
    if tool_name == "test_discover":
        return {
            "root": args.get("root", "."),
            "framework": args.get("framework", "auto"),
        }
    if tool_name == "test_run":
        return {
            "root": args.get("root", "."),
            "framework": args.get("framework", "auto"),
            "path": args.get("path", ""),
            "pattern": args.get("pattern", ""),
            "verbose": args.get("verbose", False),
            "coverage": args.get("coverage", False),
            "timeout": args.get("timeout", 120),
            "extra_args_json": args.get("extra_args_json", ""),
        }
    if tool_name == "lint_run":
        return {
            "root": args.get("root", "."),
            "tool": args.get("tool", "auto"),
            "path": args.get("path", ""),
            "fix": args.get("fix", False),
            "timeout": args.get("timeout", 60),
        }
    if tool_name == "format_code":
        return {
            "root": args.get("root", "."),
            "tool": args.get("tool", "auto"),
            "path": args.get("path", ""),
            "check_only": args.get("check_only", False),
            "timeout": args.get("timeout", 60),
        }
    if tool_name == "typecheck_run":
        return {
            "root": args.get("root", "."),
            "tool": args.get("tool", "auto"),
            "path": args.get("path", ""),
            "timeout": args.get("timeout", 120),
        }
    if tool_name == "dependency_add":
        return {
            "root": args.get("root", "."),
            "packages_json": args.get("packages_json", "[]"),
            "dev": args.get("dev", False),
            "timeout": args.get("timeout", 60),
        }
    if tool_name == "dependency_remove":
        return {
            "root": args.get("root", "."),
            "packages_json": args.get("packages_json", "[]"),
            "timeout": args.get("timeout", 60),
        }
    if tool_name == "dependency_update":
        return {
            "root": args.get("root", "."),
            "packages_json": args.get("packages_json", "[]"),
            "timeout": args.get("timeout", 120),
        }
    if tool_name == "dependency_audit":
        return {
            "root": args.get("root", "."),
            "timeout": args.get("timeout", 60),
        }
    if tool_name == "git_commit":
        return {
            "root": args.get("root", "."),
            "message": args.get("message", ""),
            "paths_json": args.get("paths_json", "[]"),
            "all_tracked": args.get("all_tracked", False),
            "timeout": args.get("timeout", 30),
        }
    if tool_name == "git_branch":
        return {
            "root": args.get("root", "."),
            "name": args.get("name", ""),
            "checkout": args.get("checkout", True),
            "base": args.get("base", ""),
            "timeout": args.get("timeout", 10),
        }
    if tool_name == "git_checkout":
        return {
            "root": args.get("root", "."),
            "ref": args.get("ref", ""),
            "timeout": args.get("timeout", 10),
        }
    if tool_name == "git_stash":
        return {
            "root": args.get("root", "."),
            "action": args.get("action", "push"),
            "message": args.get("message", ""),
            "include_untracked": args.get("include_untracked", True),
            "timeout": args.get("timeout", 10),
        }
    if tool_name == "git_tag":
        return {
            "root": args.get("root", "."),
            "name": args.get("name", ""),
            "message": args.get("message", ""),
            "delete": args.get("delete", False),
            "timeout": args.get("timeout", 10),
        }
    if tool_name == "git_merge":
        return {
            "root": args.get("root", "."),
            "branch": args.get("branch", ""),
            "no_ff": args.get("no_ff", True),
            "message": args.get("message", ""),
            "timeout": args.get("timeout", 30),
        }
    if tool_name == "git_cherry_pick":
        return {
            "root": args.get("root", "."),
            "commits_json": args.get("commits_json", "[]"),
            "timeout": args.get("timeout", 30),
        }
    if tool_name == "build_run":
        return {
            "root": args.get("root", "."),
            "command": args.get("command", ""),
            "timeout": args.get("timeout", 120),
        }
    if tool_name == "ensemble_codegen_build_loop":
        return {
            "project_dir": args.get("project_dir", "."),
            "files_json": args.get("files_json", ""),
            "build_program": args.get("build_program", ""),
            "build_args_json": args.get("build_args_json", "[]"),
            "error_regex": args.get("error_regex", ""),
            "slips_json": args.get("slips_json", ""),
            "timeout": args.get("timeout", 120),
            "extra_roots": repository_extra_roots,
        }
    if tool_name in ("build_clean", "secret_scan"):
        return {
            "root": args.get("root", "."),
            "timeout": args.get("timeout", 30),
        }
    if tool_name == "rename_symbol":
        return {
            "root": args.get("root", "."),
            "old_name": args.get("old_name", ""),
            "new_name": args.get("new_name", ""),
            "glob": args.get("glob", "**/*.py"),
            "dry_run": args.get("dry_run", True),
        }
    if tool_name == "find_references":
        return {
            "root": args.get("root", "."),
            "symbol": args.get("symbol", ""),
            "glob": args.get("glob", "**/*.py"),
        }
    if tool_name == "diff_files":
        return {
            "root": args.get("root", "."),
            "left": args.get("left", ""),
            "right": args.get("right", ""),
            "context": args.get("context", 3),
        }
    if tool_name == "apply_patch":
        return {
            "root": args.get("root", "."),
            "patch_text": args.get("patch_text", ""),
            "check_only": args.get("check_only", False),
        }
    raise KeyError(f"unsupported agent dispatch tool: {tool_name}")
