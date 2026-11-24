"""Pure path-scope, execution-argv, read-only, and claim-review guards for agents."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sonder_runtime.application.server_core.agent_prompts import (
    PROJECT_SCOPED_COMMAND_TOOLS,
    PROJECT_SCOPED_EXECUTION_TOOLS,
    PROJECT_SCOPED_PATH_TOOLS,
    PROJECT_SCOPED_ROOT_AND_PATH_TOOLS,
    REPOSITORY_READ_ONLY_FORBIDDEN_ARGS,
    REPOSITORY_READ_ONLY_TOOLS,
)
from sonder_runtime.domain import network_path_spelling as _network_spelling
from sonder_runtime.domain.agents import interpreter_argv as _interpreter_argv

_PROJECT_EXECUTION_GUARDED_TOOLS: frozenset[str] = frozenset({
    "ensemble_codegen_build_loop",
})
_PROJECT_DEFAULT_DIR_TOOLS: frozenset[str] = frozenset({
    "directory_digest", "repo_log", "repo_show", "repo_blame",
    "code_metrics", "code_graph", "workspace_inventory", "dependency_inventory",
    "directory_tree", "repository_symbol_index", "project_detect",
    "artifact_ground",
})
_READ_ONLY_FILE_PATH_TOOLS: frozenset[str] = frozenset({
    "file_read", "file_digest", "file_read_range", "image_inspect",
    "document_extract", "media_inspect", "data_inspect", "data_query",
    "log_inspect", "artifact_risk_inspect", "artifact_ground", "verify_artifact",
    "archive_list",
})
_READ_ONLY_DIR_TOOLS: frozenset[str] = frozenset({
    "workspace_inventory", "dependency_inventory", "project_detect",
    "directory_digest", "directory_tree", "file_find",
    "repository_symbol_index", "text_search", "script_search",
})


@dataclass(frozen=True)
class ReadOnlyPathCheck:
    """One path check for ``file_ops.resolve_repository_read_path``."""

    path: Any
    allow_workspace_root: bool


def scope_target_under(
    base: Path | str,
    raw_value: Any,
    *,
    is_network_spelling: Callable[[str], bool],
) -> Path | str:
    """Return ``base / raw`` for relative paths; raw value for network/absolute spellings."""
    text = str(raw_value)
    if is_network_spelling(text):
        return text
    candidate = Path(text).expanduser()
    return candidate if candidate.is_absolute() else Path(base) / candidate


def check_project_scope_paths(
    tool_name: str,
    args: Mapping[str, Any],
    project_root: str,
    *,
    dedicated: bool = True,
    reject_network_path: Callable[[str], str],
    is_foreign_absolute: Callable[[str], bool],
    is_network_spelling: Callable[[str], bool],
    batch_operations: Callable[[Mapping[str, Any]], list[Any] | None],
    context_pack_paths: Callable[[Any], Sequence[Any]],
    parse_archive_inputs: Callable[[Any], Sequence[Any]],
    parse_text_patch: Callable[[Any], Sequence[Mapping[str, Any]]],
    path_key_for: Callable[[str], str],
) -> str:
    """Validate that every path target of ``tool_name`` stays within ``project_root``.

    Returns an empty string when admitted, or a refusal detail (without the
    legacy ``ERROR: `` prefix so callers control wire formatting).
    """
    scoped_tools = PROJECT_SCOPED_PATH_TOOLS | PROJECT_SCOPED_EXECUTION_TOOLS
    if not project_root or tool_name not in scoped_tools:
        return ""
    try:
        raw_project_root = str(project_root).strip()
        try:
            if "\\??\\" in raw_project_root:
                raise PermissionError(raw_project_root)
            raw_project_root = reject_network_path(raw_project_root)
        except PermissionError:
            return "agent project path rejected: project root uses a %s" % (
                _network_spelling.REFUSAL
            )
        root = Path(raw_project_root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("project root is not a directory")
        targets: list[tuple[str, Any]]
        if not dedicated:
            if tool_name in PROJECT_SCOPED_ROOT_AND_PATH_TOOLS:
                tool_root = scope_target_under(
                    root, args.get("root") or ".", is_network_spelling=is_network_spelling,
                )
                targets = [("repository root", tool_root)]
                raw_path = str(args.get("path") or "").strip()
                if raw_path:
                    targets.append((
                        "path",
                        scope_target_under(
                            tool_root, raw_path, is_network_spelling=is_network_spelling,
                        ),
                    ))
            else:
                key = path_key_for(tool_name)
                targets = [("path", args.get(key) or ".")]
        elif tool_name == "workspace_run":
            targets = [("working directory", args.get("cwd") or ".")]
        elif tool_name == "script_run":
            targets = [("script path", args.get("path") or "")]
            if str(args.get("cwd") or "").strip():
                targets.append(("working directory", args.get("cwd")))
        elif tool_name == "data_convert":
            targets = [
                ("input path", args.get("input_path") or ""),
                ("output path", args.get("output_path") or ""),
            ]
        elif tool_name == "schema_validate":
            targets = [("path", args.get("path") or "")]
            if str(args.get("schema_path") or "").strip():
                targets.append(("schema path", args.get("schema_path")))
        elif tool_name == "code_graph":
            targets = [("root", args.get("root") or ".")]
        elif tool_name in {"symbol_verify", "citation_audit", "error_diagnose"}:
            targets = []
            if str(args.get("root") or "").strip():
                targets.append(("root", args.get("root")))
        elif tool_name in {"prompt_shield", "code_audit", "data_transform"}:
            targets = []
            if str(args.get("path") or "").strip():
                targets.append(("path", args.get("path")))
        elif tool_name == "diff_semantic":
            targets = []
            if str(args.get("path_a") or "").strip():
                targets.append(("path_a", args.get("path_a")))
            if str(args.get("path_b") or "").strip():
                targets.append(("path_b", args.get("path_b")))
        elif tool_name == "repo_diff":
            targets = [("repository root", args.get("root") or ".")]
            if str(args.get("path") or "").strip():
                targets.append(("diff path", args.get("path")))
        elif tool_name == "diff_files":
            targets = [
                ("repository root", args.get("root") or "."),
                ("left path", args.get("left") or ""),
                ("right path", args.get("right") or ""),
            ]
        elif tool_name in PROJECT_SCOPED_ROOT_AND_PATH_TOOLS:
            tool_root = scope_target_under(
                root, args.get("root") or ".", is_network_spelling=is_network_spelling,
            )
            targets = [("repository root", tool_root)]
            raw_path = str(args.get("path") or "").strip()
            if raw_path:
                targets.append((
                    "path",
                    scope_target_under(
                        tool_root, raw_path, is_network_spelling=is_network_spelling,
                    ),
                ))
        elif tool_name == "file_batch_write":
            operations = batch_operations(args)
            if operations is None or not operations:
                return "agent project path rejected: operations must be a non-empty JSON list"
            targets = []
            for index, operation in enumerate(operations):
                if not isinstance(operation, dict):
                    return "agent project path rejected: operation %d must be an object" % index
                targets.append(("operation %d path" % index, operation.get("path") or ""))
        elif tool_name == "context_pack":
            targets = [
                ("path", path)
                for path in context_pack_paths(
                    args.get("paths_json", args.get("paths", []))
                )
            ]
        elif tool_name == "archive_create":
            archive_root = scope_target_under(
                root, args.get("root") or ".", is_network_spelling=is_network_spelling,
            )
            targets = [("archive root", archive_root)]
            try:
                archive_inputs = parse_archive_inputs(
                    args.get("inputs_json", args.get("inputs", []))
                )
            except ValueError as exc:
                return "agent project archive inputs rejected: %s" % exc
            for raw_input in archive_inputs:
                targets.append((
                    "archive input",
                    scope_target_under(
                        archive_root, raw_input, is_network_spelling=is_network_spelling,
                    ),
                ))
            destination_text = str(args.get("destination") or "").strip()
            if not destination_text:
                targets.append(("archive destination", ""))
            else:
                targets.append((
                    "archive destination",
                    scope_target_under(
                        archive_root, destination_text,
                        is_network_spelling=is_network_spelling,
                    ),
                ))
        elif tool_name in {"file_copy", "file_move"}:
            targets = [
                ("source", args.get("source") or ""),
                ("destination", args.get("destination") or ""),
            ]
        elif tool_name == "archive_extract":
            targets = [
                ("archive source", args.get("source") or ""),
                ("archive destination", args.get("destination") or ""),
            ]
        elif tool_name == "text_patch":
            targets = [("root", args.get("root") or ".")]
            try:
                targets.extend(
                    ("patch path", item["path"])
                    for item in parse_text_patch(args.get("patch", ""))
                )
            except (TypeError, ValueError, PermissionError) as exc:
                return "agent project path rejected: invalid patch: %s" % exc
        elif tool_name == "workspace_compare":
            targets = [
                ("left path", args.get("left") or ""),
                ("right path", args.get("right") or ""),
            ]
        else:
            key = path_key_for(tool_name)
            targets = [("path", args.get(key) or ".")]

        for label, raw_value in targets:
            raw = str(raw_value or "").strip()
            if not raw:
                return "agent project path rejected: %s is required" % label
            try:
                if "\\??\\" in raw:
                    raise PermissionError(raw)
                raw = reject_network_path(raw)
            except PermissionError:
                return "agent project path rejected: %s uses a %s" % (
                    label, _network_spelling.REFUSAL,
                )
            if is_foreign_absolute(raw):
                return (
                    "agent project path rejected: %s uses a non-native or drive-relative path"
                    % label
                )
            target = Path(raw.replace("\\", "/") if os.name != "nt" else raw).expanduser()
            if not target.is_absolute():
                target = root / target
            if label == "diff path":
                target = target.parent.resolve(strict=False) / target.name
            else:
                target = target.resolve(strict=False)
            try:
                target.relative_to(root)
            except ValueError:
                return (
                    "agent project path rejected: %s is outside the "
                    "host-selected project root" % label
                )
    except (OSError, TypeError, ValueError) as exc:
        return "agent project scope is invalid: %s" % exc
    return ""


def argv_value_outside_root(raw: str, base: Path, root: Path) -> bool:
    """Return whether one argv value names a path outside ``root``."""
    drive_match = re.search(r"[A-Za-z]:[\\/]", raw)
    if drive_match:
        raw = raw[drive_match.start():]
    if (
        (
            os.name == "nt"
            and re.fullmatch(r"/[A-Za-z][A-Za-z0-9:+\-]*", raw)
            and not os.path.lexists(str(base / raw))
        )
        or (
            os.name != "nt"
            and ".." not in raw
            and re.fullmatch(r"/[A-Za-z][A-Za-z0-9+\-]*:[A-Za-z0-9_.=+\-]+", raw)
            and not os.path.lexists(raw)
        )
    ):
        return False
    drive_relative = os.name == "nt" and bool(re.fullmatch(r"[A-Za-z]:[^:=]+", raw))
    path_like = (
        drive_relative
        or os.path.isabs(raw)
        or raw.startswith((".", "~"))
        or "/" in raw
        or "\\" in raw
        or bool(os.path.splitext(raw)[1])
    )
    if not path_like:
        return False
    target = Path(raw.replace("\\", "/") if os.name != "nt" else raw).expanduser()
    if not target.is_absolute():
        target = base / target
    try:
        target.resolve(strict=False).relative_to(root)
    except (OSError, ValueError):
        return True
    return False


def program_token_escape_detail(
    raw_program: Any,
    base: Path,
    root: Path | None,
    *,
    check_outside: bool,
    python_executable: str,
) -> str:
    """Return refusal detail (without ``ERROR: ``) for unsafe program paths, or ``""``."""
    token = str(raw_program or "").strip().strip('"\'').lstrip("@=,;")
    if not token:
        return ""
    if _network_spelling.contains_network_spelling(token):
        return (
            "agent project execution rejected: the program path uses a %s"
            % _network_spelling.REFUSAL
        )
    if not check_outside or root is None:
        return ""
    looks_like_path = (
        token.startswith((".", "~"))
        or "/" in token
        or "\\" in token
        or (os.name == "nt" and bool(re.match(r"^[A-Za-z]:", token)))
    )
    if not looks_like_path:
        return ""
    target = Path(token.replace("\\", "/") if os.name != "nt" else token).expanduser()
    if not target.is_absolute():
        target = base / target
    try:
        resolved = target.resolve(strict=False)
        if os.path.normcase(str(resolved)) == os.path.normcase(
            os.path.realpath(python_executable)
        ):
            return ""
        resolved.relative_to(root)
    except (OSError, ValueError):
        return (
            "agent project execution rejected: argv path is outside "
            "the host-selected project root"
        )
    return ""


def check_project_execution_arguments(
    tool_name: str,
    args: Any,
    project_root: str,
    *,
    reject_network_path: Callable[[str], str],
    extract_argv: Callable[[Mapping[str, Any]], Sequence[str]],
    python_executable: str,
) -> str:
    """Reject project-execution escape paths and inline interpreters.

    Returns empty string when admitted, or refusal detail without ``ERROR: ``.
    """
    guarded = (
        PROJECT_SCOPED_EXECUTION_TOOLS
        | PROJECT_SCOPED_COMMAND_TOOLS
        | _PROJECT_EXECUTION_GUARDED_TOOLS
    )
    if tool_name not in guarded or not isinstance(args, dict):
        return ""
    has_project_root = bool(str(project_root or "").strip())
    if not has_project_root and tool_name not in PROJECT_SCOPED_COMMAND_TOOLS:
        return ""
    root: Path | None = None
    if has_project_root:
        try:
            root = Path(str(project_root)).expanduser().resolve(strict=True)
        except (OSError, ValueError) as exc:
            return "agent project scope is invalid: %s" % exc
    stdin_present = bool(str(args.get("stdin") or ""))
    is_windows = os.name == "nt"
    program_tokens: list[str] = []
    if tool_name in PROJECT_SCOPED_COMMAND_TOOLS:
        plan = _interpreter_argv.analyze_build_command(
            str(args.get("command") or ""),
            stdin_present=stdin_present,
            is_windows=is_windows,
        )
        if plan.opaque_reason:
            return "agent project execution rejected: %s" % plan.opaque_reason
        if plan.launcher:
            return (
                "agent project execution rejected: %s is a command-string "
                "launcher (it decodes and runs a command line of its own), which is "
                "outside the project path guard" % plan.launcher
            )
        if plan.inline_violation:
            return (
                "agent project execution rejected: inline interpreter "
                "commands are outside the project path guard"
            )
        if plan.stdin_violation:
            return (
                "agent project execution rejected: interpreter code via "
                "stdin is outside the project path guard"
            )
        argv = list(plan.path_tokens)
        program_tokens = list(plan.program_tokens)
        base_value: Any = args.get("root") or "."
    else:
        if tool_name == "ensemble_codegen_build_loop":
            program_raw = str(args.get("build_program") or "")
            argv = list(extract_argv({"args_json": args.get("build_args_json", "[]")}))
            base_value = args.get("project_dir") or "."
            stdin_present = False
        else:
            program_raw = str(args.get("program") or "")
            argv = list(extract_argv(args))
            base_value = args.get("cwd") or (
                os.path.dirname(str(args.get("path") or ""))
                if tool_name == "script_run"
                else "."
            )
        if program_raw:
            program_tokens = [program_raw]
        verdict, detail = _interpreter_argv.analyze_argv_command(
            program_raw, argv, stdin_present=stdin_present,
        )
        if verdict == "launcher":
            return (
                "agent project execution rejected: %s is a command-string "
                "launcher (it decodes and runs a command line of its own), which is "
                "outside the project path guard" % detail
            )
        if verdict == "inline":
            return (
                "agent project execution rejected: inline interpreter "
                "commands are outside the project path guard"
            )
        if verdict == "stdin":
            return (
                "agent project execution rejected: interpreter code via "
                "stdin is outside the project path guard"
            )
    try:
        base_value = reject_network_path(str(base_value))
    except PermissionError:
        return (
            "agent project execution rejected: the working directory uses a %s"
            % _network_spelling.REFUSAL
        )
    try:
        base = Path(str(base_value)).expanduser().resolve(strict=False)
    except (OSError, ValueError) as exc:
        return "agent project execution rejected: working directory: %s" % exc
    check_prog_outside = (tool_name in PROJECT_SCOPED_COMMAND_TOOLS) and (root is not None)
    for prog_tok in program_tokens:
        prog_err = program_token_escape_detail(
            prog_tok, base, root,
            check_outside=check_prog_outside,
            python_executable=python_executable,
        )
        if prog_err:
            return prog_err
    for raw_item in argv:
        net_bad, candidates = _interpreter_argv.expand_argv_path_candidates(
            raw_item, is_windows=is_windows,
        )
        if net_bad:
            return (
                "agent project execution rejected: an argv path uses a %s"
                % _network_spelling.REFUSAL
            )
        if root is not None and any(
            argv_value_outside_root(value, base, root) for value in candidates
        ):
            return (
                "agent project execution rejected: argv path is outside "
                "the host-selected project root"
            )
    return ""


def scope_project_tool_args(
    tool_name: str,
    args: Any,
    project: str,
    *,
    dedicated: bool = True,
    scoped_tools: frozenset[str],
    is_rooted: Callable[[str], bool],
    path_key_for: Callable[[str], str],
    batch_operations: Callable[[Mapping[str, Any]], list[Any] | None],
    context_pack_paths: Callable[[Any], Sequence[str]],
    trusted_approval: Any,
) -> Any:
    """Rebase relative tool arguments onto ``project`` and bind ``extra_roots``."""
    if not project or not isinstance(args, dict) or tool_name not in scoped_tools:
        return args
    scoped: dict[str, Any] = dict(args)
    if tool_name == "file_read":
        scoped["path"] = (
            scoped.get("path")
            or scoped.get("file")
            or scoped.get("filename")
            or scoped.get("file_path")
            or ""
        )
    scoped["extra_roots"] = project
    if dedicated and tool_name == "text_patch":
        scoped["approval"] = trusted_approval
    if not dedicated:
        key = path_key_for(tool_name)
        raw = str(scoped.get(key) or "").strip()
        if raw == ".":
            scoped[key] = project
        elif not raw:
            if key == "root" or tool_name in _PROJECT_DEFAULT_DIR_TOOLS:
                scoped[key] = project
        elif not is_rooted(raw):
            scoped[key] = os.path.join(project, raw)
        return scoped

    if tool_name == "data_convert":
        for key in ("input_path", "output_path"):
            raw_path = str(scoped.get(key) or "").strip()
            if raw_path and not is_rooted(raw_path):
                scoped[key] = os.path.normpath(os.path.join(project, raw_path))
        return scoped

    if tool_name == "schema_validate":
        for key in ("path", "schema_path"):
            raw_path = str(scoped.get(key) or "").strip()
            if raw_path and not is_rooted(raw_path):
                scoped[key] = os.path.normpath(os.path.join(project, raw_path))
        return scoped

    if tool_name == "diff_semantic":
        for key in ("path_a", "path_b"):
            raw_path = str(scoped.get(key) or "").strip()
            if raw_path and not is_rooted(raw_path):
                scoped[key] = os.path.normpath(os.path.join(project, raw_path))
        return scoped

    if tool_name == "file_batch_write":
        operations = batch_operations(scoped)
        if operations is None:
            return scoped
        rebased_ops: list[Any] = []
        for operation in operations:
            if not isinstance(operation, dict):
                rebased_ops.append(operation)
                continue
            item = dict(operation)
            raw_path = str(item.get("path") or "").strip()
            if raw_path and not is_rooted(raw_path):
                item["path"] = os.path.normpath(os.path.join(project, raw_path))
            rebased_ops.append(item)
        scoped["operations_json"] = json.dumps(rebased_ops, ensure_ascii=False)
        scoped.pop("operations", None)
        return scoped

    if tool_name == "context_pack":
        try:
            paths = context_pack_paths(
                scoped.get("paths_json", scoped.get("paths", []))
            )
        except ValueError:
            return scoped
        scoped["paths_json"] = [
            raw_path if is_rooted(raw_path) else os.path.join(project, raw_path)
            for raw_path in paths
        ]
        scoped.pop("paths", None)
        return scoped

    if tool_name in {"file_copy", "file_move", "archive_extract"}:
        for key in ("source", "destination"):
            raw_path = str(scoped.get(key) or "").strip()
            if raw_path and not is_rooted(raw_path):
                scoped[key] = os.path.join(project, raw_path)
        return scoped

    if tool_name == "workspace_compare":
        for key in ("left", "right"):
            raw_path = str(scoped.get(key) or "").strip()
            if raw_path and not is_rooted(raw_path):
                scoped[key] = os.path.join(project, raw_path)
        return scoped

    if tool_name == "workspace_run":
        raw_cwd = str(scoped.get("cwd") or ".").strip()
        scoped["cwd"] = raw_cwd if is_rooted(raw_cwd) else os.path.join(project, raw_cwd)
        return scoped

    if tool_name == "script_run":
        raw_path = str(scoped.get("path") or "").strip()
        if raw_path and not is_rooted(raw_path):
            scoped["path"] = os.path.join(project, raw_path)
        raw_cwd = str(scoped.get("cwd") or "").strip()
        if raw_cwd and not is_rooted(raw_cwd):
            scoped["cwd"] = os.path.join(project, raw_cwd)
        return scoped

    if tool_name == "output_digest":
        raw_path = str(scoped.get("path") or "").strip()
        if raw_path and not is_rooted(raw_path):
            scoped["path"] = os.path.normpath(os.path.join(project, raw_path))
        return scoped

    if tool_name == "ensemble_codegen_build_loop":
        raw_project = str(scoped.get("project_dir") or ".").strip()
        scoped["project_dir"] = (
            raw_project if is_rooted(raw_project) else os.path.join(project, raw_project)
        )
        return scoped

    key = path_key_for(tool_name)
    raw = str(scoped.get(key) or "").strip()
    if raw == ".":
        scoped[key] = project
    elif not raw:
        if key == "root" or tool_name in _PROJECT_DEFAULT_DIR_TOOLS:
            scoped[key] = project
    elif not is_rooted(raw):
        scoped[key] = os.path.join(project, raw)
    return scoped


def check_read_only_preconditions(
    tool_name: str,
    args: Any,
    *,
    trusted_extra_roots: str,
    git_ignore_discovery_tools: frozenset[str],
) -> str:
    """Validate structural/policy preconditions for a repository read-only call."""
    if not isinstance(args, dict):
        return "repository read-only tool args must be a JSON object."
    if tool_name not in REPOSITORY_READ_ONLY_TOOLS:
        return "tool '%s' is not allowed by the repository read-only policy." % tool_name
    if tool_name in git_ignore_discovery_tools and args.get("include_ignored"):
        return (
            "repository read-only tool '%s' forbids include_ignored=true."
            % tool_name
        )
    forbidden = sorted(
        name
        for name in REPOSITORY_READ_ONLY_FORBIDDEN_ARGS.intersection(args)
        if not (
            name == "extra_roots"
            and trusted_extra_roots
            and args.get("extra_roots") == trusted_extra_roots
        )
    )
    if forbidden:
        return (
            "repository read-only tool '%s' forbids argument(s): %s."
            % (tool_name, ", ".join(forbidden))
        )
    return ""


def collect_read_only_path_checks(
    tool_name: str,
    args: Mapping[str, Any],
    *,
    context_pack_paths: Callable[[Any], Sequence[Any]],
) -> tuple[list[ReadOnlyPathCheck], str]:
    """Return ``(path_checks, special_adapter_check)`` for ``_repository_read_only_error``.

    ``special_adapter_check`` is one of ``""``, ``"repo_diff_or_status"``,
    ``"git_history"``, or ``"root_confined_read"``.
    """
    if tool_name in _READ_ONLY_FILE_PATH_TOOLS:
        return [ReadOnlyPathCheck(args.get("path", ""), False)], ""
    if tool_name == "schema_validate":
        checks = [ReadOnlyPathCheck(args.get("path", ""), False)]
        if str(args.get("schema_path") or "").strip():
            checks.append(ReadOnlyPathCheck(args.get("schema_path", ""), False))
        return checks, ""
    if tool_name == "code_metrics":
        return [ReadOnlyPathCheck(args.get("path", ".") or ".", True)], ""
    if tool_name == "code_graph":
        return [ReadOnlyPathCheck(args.get("root", ".") or ".", True)], ""
    if tool_name in {"symbol_verify", "citation_audit", "error_diagnose"}:
        if str(args.get("root") or "").strip():
            return [ReadOnlyPathCheck(args.get("root", ""), True)], ""
        return [], ""
    if tool_name in {"prompt_shield", "code_audit", "data_transform", "output_digest"}:
        if str(args.get("path") or "").strip():
            return [ReadOnlyPathCheck(args.get("path", ""), False)], ""
        return [], ""
    if tool_name == "diff_semantic":
        checks = [
            ReadOnlyPathCheck(args.get(key, ""), False)
            for key in ("path_a", "path_b")
            if str(args.get(key) or "").strip()
        ]
        return checks, ""
    if tool_name in {"repo_status", "repo_diff"}:
        return [], "repo_diff_or_status"
    if tool_name == "context_pack":
        return [
            ReadOnlyPathCheck(path, False)
            for path in context_pack_paths(args.get("paths_json", args.get("paths", [])))
        ], ""
    if tool_name in {"repo_log", "repo_show", "repo_blame"}:
        return [], "git_history"
    if tool_name == "workspace_compare":
        return [
            ReadOnlyPathCheck(args.get("left", ""), True),
            ReadOnlyPathCheck(args.get("right", ""), True),
        ], ""
    if tool_name in {"find_references", "diff_files"}:
        return [], "root_confined_read"
    if tool_name in _READ_ONLY_DIR_TOOLS:
        return [
            ReadOnlyPathCheck(args.get("path", "") or args.get("root", "") or ".", True)
        ], ""
    return [], ""


def parse_claim_review_payload(
    payload: Any,
    vocabulary: Sequence[str],
) -> dict[str, Any]:
    """Validate and normalize a negative-claim reviewer JSON payload."""
    if not isinstance(payload, dict):
        raise ValueError("claim review must be a JSON object")
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in {"accept", "continue"}:
        raise ValueError("claim review decision must be accept or continue")
    reason = re.sub(r"\s+", " ", str(payload.get("reason") or "")).strip()
    tool = str(payload.get("tool") or "").strip()
    args = payload.get("args") or {}
    if not reason:
        raise ValueError("claim review needs a reason")
    if not isinstance(args, dict):
        raise ValueError("claim review args must be a JSON object")
    if decision == "continue" and tool and tool not in vocabulary:
        raise ValueError(
            "continued claim review needs one of these approved "
            "read-only tools: %s" % ", ".join(vocabulary)
        )
    if decision == "accept":
        tool, args = "", {}
    return {
        "decision": decision,
        "reason": reason[:500],
        "tool": tool,
        "args": args,
    }
