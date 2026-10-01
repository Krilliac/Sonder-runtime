"""Legacy MCP registration for the bounded ``file_check`` agent tool."""
from __future__ import annotations

import time
import re
from pathlib import Path
from collections.abc import Callable, Mapping

from ..adapters.code_check import check_file, is_checkable, check_budget
from ..domain.agents.tool_help import tool_advertised
from ..domain.cloud_access import LEGACY_ERROR_PREFIX


_EDIT_TOOLS = frozenset(("file_edit", "file_write", "text_patch", "apply_patch"))


def _edited_path(tool: str, args: Mapping) -> str | None:
    """Extract one explicit edit target; patch bodies are never executed."""
    if tool not in _EDIT_TOOLS:
        return None
    for key in ("path", "file", "filename", "file_path"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if tool in ("apply_patch", "text_patch"):
        patch = args.get("patch") or args.get("patch_text") or args.get("content")
        if isinstance(patch, str):
            for line in patch.splitlines():
                text = line.strip()
                if text.startswith("*** Update File:") or text.startswith("*** Add File:"):
                    return text.split(":", 1)[1].strip().strip('"')
                if text.startswith("+++ "):
                    value = text[4:].split("\t", 1)[0].strip()
                    if value != "/dev/null":
                        return value[2:] if value.startswith(("a/", "b/")) else value
    return None


def _edited_paths(tool: str, args: Mapping) -> list[str]:
    first = _edited_path(tool, args)
    paths = [first] if first else []
    if tool in ("apply_patch", "text_patch"):
        patch = args.get("patch") or args.get("patch_text") or args.get("content")
        if isinstance(patch, str):
            for line in patch.splitlines():
                text = line.strip()
                value = None
                if text.startswith("*** Update File:") or text.startswith("*** Add File:"):
                    value = text.split(":", 1)[1].strip().strip('"')
                elif text.startswith("+++ "):
                    value = text[4:].split("\t", 1)[0].strip()
                    if value.startswith(("a/", "b/")):
                        value = value[2:]
                if value and value != "/dev/null" and value not in paths:
                    paths.append(value)
    if tool in {"text_patch", "apply_patch"}:
        paths = [str(Path(args.get("root") or ".") / path) for path in paths]
    return paths


def post_edit_file_check(
    tool: str,
    args: Mapping,
    observation: str,
    dispatch: Callable[[str, dict], str],
    *,
    gate: Callable[[str, Mapping], bool] | None = None,
) -> str | None:
    """Run the already-authorized check after a successful file mutation.

    ``dispatch`` and ``gate`` are injected by the server so this helper cannot
    bypass permission evaluation or invent an extra root. It only handles a
    single explicit checkable target and never interprets patch contents.
    """
    if tool not in _EDIT_TOOLS or not isinstance(observation, str) or not observation:
        return observation
    if (observation.startswith("ERROR") or re.search(r"\bok[\"']?\s*:\s*(?:False|false)\b", observation)
            or args.get("dry_run") or args.get("check_only")
            or (tool == "text_patch" and args.get("apply") is not True)):
        return observation
    paths = [path for path in _edited_paths(tool, args) if is_checkable(path)]
    if not paths:
        return observation
    if not tool_advertised("file_check"):
        return observation + "\nfile_check: skipped (outside this run's allowlist)"
    additions, diagnostics = [], []
    issue_count = 0
    checked = False
    with check_budget() as deadline:
        for path in paths:
            payload = {"path": path, "max_items": 30}
            if time.monotonic() >= deadline:
                additions.append("file_check: skipped (check budget exhausted)")
                break
            if gate is not None and not gate("file_check", payload):
                additions.append("file_check: unavailable (permission required)")
                break
            try:
                result = dispatch("file_check", payload)
            except Exception as exc:
                additions.append("file_check: unavailable (%s)" % type(exc).__name__)
                continue
            rows = str(result).splitlines()
            if not rows or rows[0].startswith("ERROR"):
                additions.append("file_check: unavailable (" + (rows[0] if rows else "no result") + ")")
            elif rows[0].endswith(": none"):
                checked = True
            else:
                count = re.search(r": (\d+) issue\(s\)$", rows[0])
                if count:
                    checked = True
                    issue_count += int(count[1])
                    prefix = str(path) + ":" if len(paths) > 1 else ""
                    diagnostics.extend(prefix + row for row in rows[1:1 + max(0, 5 - len(diagnostics))])
                else:
                    additions.append("file_check: unavailable (unrecognized check result)")
    summary = ([f"file_check: {issue_count} issue(s)", *diagnostics] if issue_count else
               ["file_check: none"] if checked else [])
    additions = summary + additions
    return observation + ("\n" + "\n".join(additions)[:600] if additions else "")


def dispatch_file_check(path, max_items=30, *, project_root=None):
    # Fail closed if integration omitted the execution-class registration.
    from ..adapters.security.permission_policy import permission_policy
    if "file_check" not in permission_policy.EXECUTION_TOOLS:
        return f"{LEGACY_ERROR_PREFIX} file_check execution policy registration is missing"
    return check_file(path, max_items, project_root=project_root)


def native_agent_assist(name, arguments, registry, roots):
    """Native transport handlers, entered after its schema and permission gate."""
    if name == "tool_help":
        from ..domain.agents.tool_help import tool_help_text
        catalog = {item.name: {"description": item.description, "input_schema": item.input_schema}
                   for item in registry.list_all()}
        output = tool_help_text(**arguments, catalog=catalog)
    else:
        candidate = Path(arguments["path"]).expanduser()
        project = None
        for root in roots:
            resolved = Path(root).resolve()
            target = (candidate if candidate.is_absolute() else resolved / candidate).resolve()
            if target.is_relative_to(resolved):
                project = resolved
                break
        output = (dispatch_file_check(arguments["path"], arguments.get("max_items", 30), project_root=project)
                  if project is not None else f"{LEGACY_ERROR_PREFIX} file_check path is outside the native workspace roots")
    failed = output.startswith(LEGACY_ERROR_PREFIX)
    return {"output": output, "isError": failed, "error": "tool_refused" if failed else None,
            "evidence": {"tool": name}}


def register(mcp, record, project_root=None) -> None:
    """Register ``file_check`` without probing the filesystem at startup."""

    @mcp.tool()
    def file_check(path: str, max_items: int = 30) -> str:
        """Check one source file for syntax and available local diagnostics."""
        started = time.time()
        args = {"path": path, "max_items": max_items}
        try:
            trusted_root = project_root() if callable(project_root) else project_root
            output = dispatch_file_check(path, max_items, project_root=trusted_root)
            ok = ": none" in output
            summary = output.splitlines()[0][:200]
        except Exception as exc:  # a tool reports failures rather than taking down MCP
            output = ("file_check %s: 1 issue(s)\n1:1: file_check error %s" % (path, exc))[:2000]
            ok, summary = False, str(exc)[:200]
        if record is not None:
            record("file_check", args, ok=ok, started=started, summary=summary, output=output)
        return output


__all__ = ["register"]
