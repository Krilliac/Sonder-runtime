"""Pure workspace, task-dispatch, static test-discovery, and unload report helpers."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class ContextPackResult:
    """Assembled bounded context pack output and accounting."""

    output: str
    selected_count: int
    emitted_bytes: int
    error_count: int
    pack_truncated: bool


def assemble_context_pack(
    paths: Sequence[str],
    *,
    file_limit: int,
    total_limit: int,
    per_file_limit: int,
    section_prefix: str,
    read_file_fn: Callable[[str, int], Mapping[str, Any]],
    utf8_prefix_fn: Callable[[str, int], tuple[str, int, bool]],
) -> ContextPackResult:
    """Read and pack guarded text files within deterministic byte and file caps."""
    selected = list(paths[:file_limit])
    omitted = max(0, len(paths) - len(selected))
    remaining = total_limit
    emitted = 0
    errors = 0
    truncated_files = 0
    sections: list[str] = []

    for index, requested in enumerate(selected, 1):
        display = requested.replace("\r", "\\r").replace("\n", "\\n")
        header = "%s%d/%d: %s =====" % (section_prefix, index, len(selected), display)
        if remaining <= 0:
            truncated_files += 1
            sections.append(
                "\n".join(
                    [
                        header,
                        "status: skipped",
                        "included-bytes: 0",
                        "truncated: true (total byte budget exhausted)",
                    ]
                )
            )
            continue
        read_limit = min(per_file_limit, remaining)
        try:
            data = read_file_fn(requested, read_limit)
            body, body_bytes, decode_truncated = utf8_prefix_fn(
                str(data.get("text", "")),
                read_limit,
            )
            source_bytes = int(data.get("bytes", 0))
            was_truncated = bool(data.get("truncated")) or decode_truncated
            reason = ""
            if was_truncated:
                truncated_files += 1
                reason = (
                    "total byte budget"
                    if read_limit < per_file_limit
                    else "per-file byte cap"
                )
            emitted += body_bytes
            remaining -= body_bytes
            lines = [
                header,
                "status: ok",
                "source-bytes: %d" % source_bytes,
                "included-bytes: %d" % body_bytes,
                "truncated: %s%s"
                % (
                    "true" if was_truncated else "false",
                    " (%s)" % reason if reason else "",
                ),
                "",
                body,
            ]
            sections.append("\n".join(lines))
        except Exception as exc:
            errors += 1
            sections.append(
                "\n".join(
                    [
                        header,
                        "status: error",
                        "included-bytes: 0",
                        "truncated: false",
                        "error: %s: %s" % (type(exc).__name__, exc),
                    ]
                )
            )

    pack_truncated = bool(omitted or truncated_files)
    summary = (
        "context pack: requested=%d selected=%d emitted-bytes=%d "
        "max-files=%d max-total-bytes=%d max-bytes-per-file=%d errors=%d"
        % (
            len(paths),
            len(selected),
            emitted,
            file_limit,
            total_limit,
            per_file_limit,
            errors,
        )
    )
    truncation = "pack-truncated: %s" % ("true" if pack_truncated else "false")
    if omitted:
        truncation += " (%d file(s) omitted by max-files)" % omitted
    output = "\n\n".join([summary, truncation] + sections)
    return ContextPackResult(
        output=output,
        selected_count=len(selected),
        emitted_bytes=emitted,
        error_count=errors,
        pack_truncated=pack_truncated,
    )


_STATIC_TEST_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        "target",
        "dist",
        "build",
        ".venv",
        "venv",
        ".mypy_cache",
        ".ruff_cache",
    }
)


def static_discover_tests(
    resolved: Path,
    framework: str = "auto",
    *,
    is_reparse_point: Callable[[Path], bool],
    is_credential_store: Callable[[Path], bool],
    max_files: int = 500,
) -> dict[str, Any]:
    """Discover tests via static filename and AST/regex scanning without execution."""
    fw = str(framework or "auto").strip().lower() or "auto"
    if fw == "auto":
        if (resolved / "Cargo.toml").is_file():
            fw = "cargo"
        elif (resolved / "go.mod").is_file():
            fw = "go"
        elif any(resolved.glob("*.csproj")) or any(resolved.glob("*.sln")):
            fw = "dotnet"
        elif (resolved / "vitest.config.ts").is_file() or (
            resolved / "vitest.config.js"
        ).is_file():
            fw = "vitest"
        elif (resolved / "jest.config.js").is_file() or (
            resolved / "jest.config.ts"
        ).is_file():
            fw = "jest"
        else:
            fw = "pytest"

    test_files: list[str] = []
    test_count = 0

    for dirpath, dirnames, filenames in os.walk(resolved, followlinks=False):
        cur = Path(dirpath)
        kept: list[str] = []
        for d in sorted(dirnames):
            if d in _STATIC_TEST_SKIP_DIRS or d.startswith("."):
                continue
            cand = cur / d
            if cand.is_symlink() or is_reparse_point(cand):
                continue
            if is_credential_store(cand):
                continue
            kept.append(d)
        dirnames[:] = kept
        for fname in sorted(filenames):
            if len(test_files) >= max_files:
                break
            fpath = cur / fname
            if fpath.is_symlink() or is_reparse_point(fpath):
                continue
            if is_credential_store(fpath):
                continue
            rel = fpath.relative_to(resolved).as_posix()
            lower = fname.lower()
            is_py_test = lower.endswith(".py") and (
                lower.startswith("test_") or lower.endswith("_test.py")
            )
            is_js_test = bool(
                re.search(r"\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$", lower)
            )
            is_go_test = lower.endswith("_test.go")
            is_rs_test = lower.endswith(".rs") and (
                "/tests/" in ("/" + rel) or lower.endswith("_test.rs")
            )
            is_cs_test = lower.endswith("tests.cs") or lower.endswith("test.cs")
            matched = (
                (fw in ("pytest", "unittest") and is_py_test)
                or (fw in ("jest", "vitest") and is_js_test)
                or (fw == "go" and is_go_test)
                or (fw == "cargo" and is_rs_test)
                or (fw == "dotnet" and is_cs_test)
            )
            if not matched:
                continue
            test_files.append(rel)
            try:
                raw = fpath.read_bytes()[:262144].decode("utf-8", errors="replace")
            except OSError:
                continue
            if is_py_test:
                try:
                    tree = ast.parse(raw, filename=rel)
                    test_count += sum(
                        1
                        for node in ast.walk(tree)
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name.startswith("test_")
                    )
                except SyntaxError:
                    pass
            elif is_js_test:
                test_count += len(re.findall(r"\b(?:it|test)\s*\(", raw))
            elif is_go_test:
                test_count += len(
                    re.findall(r"^\s*func\s+Test\w*\s*\(", raw, re.MULTILINE)
                )
            elif is_rs_test:
                test_count += len(re.findall(r"#\[\s*(?:tokio::)?test\b", raw))
            elif is_cs_test:
                test_count += len(
                    re.findall(r"\[\s*(?:Fact|Theory|Test|TestMethod)\b", raw)
                )
        if len(test_files) >= max_files:
            break

    return {
        "framework": fw,
        "test_count": test_count,
        "test_files": test_files,
        "static_only": True,
    }


def dispatch_scoped_task_tool(
    service: Any,
    tool_name: str,
    values: dict[str, Any],
    *,
    account_scope: str,
    format_task: Callable[[dict[str, Any]], str],
    format_checklist: Callable[[dict[str, Any]], str],
    normalize_checklist_items: Callable[[Any], Iterable[tuple[str, str]]],
) -> str:
    """Dispatch one account-scoped task or checklist tool operation."""
    if tool_name == "task_create":
        row = service.create_task(account_scope=account_scope, **values)
        return "task created\n  " + format_task(row.to_dict())
    if tool_name == "task_list":
        rows = service.list_tasks(account_scope=account_scope, **values)
        lines = ["sonder tasks"]
        lines.extend("  " + format_task(row.to_dict()) for row in rows)
        return "\n".join(lines) if rows else "\n".join(lines + ["  (no matching tasks)"])
    if tool_name == "task_update":
        task_id = values.pop("task_id")
        row = service.update_task(task_id, account_scope=account_scope, **values)
        return "task updated\n  " + format_task(row.to_dict())
    if tool_name == "task_show":
        task_id = values.pop("task_id")
        include_events = values.pop("events", True)
        detail = service.show_task(
            task_id,
            include_events=bool(include_events),
            account_scope=account_scope,
            **values,
        )
        if not detail.task:
            return "no task '%s'." % task_id
        lines = ["task", "  " + format_task(detail.task.to_dict())]
        if detail.events:
            lines.append("events:")
            lines.extend("  %(ts)s  %(event)s  %(note)s" % event for event in detail.events)
        return "\n".join(lines)
    if tool_name == "task_delete":
        result = service.delete_task(values.pop("task_id"), account_scope=account_scope)
        return "deleted task %s (removed %d children)" % (
            result["deleted"][:8],
            result["children_removed"],
        )
    if tool_name == "task_plan":
        raw_steps = values.pop("steps")
        steps = json.loads(raw_steps) if isinstance(raw_steps, str) else raw_steps
        checklist = service.plan_tasks(
            account_scope=account_scope, steps=steps, **values
        )
        return format_checklist(checklist.to_dict())
    if tool_name == "task_progress":
        stats = service.task_progress(account_scope=account_scope, **values)
        bar_len = 20
        filled = round(bar_len * stats["progress_pct"] / 100)
        bar = "#" * filled + "-" * (bar_len - filled)
        project = values.get("project", "")
        return "\n".join(
            [
                "sonder task progress%s" % (" [%s]" % project if project else ""),
                "  [%s] %.1f%%" % (bar, stats["progress_pct"]),
                "  total: %d | pending: %d | in_progress: %d | blocked: %d | done: %d | canceled: %d"
                % (
                    stats["total"],
                    stats["pending"],
                    stats["in_progress"],
                    stats["blocked"],
                    stats["done"],
                    stats["canceled"],
                ),
            ]
        )
    if tool_name == "task_ledger":
        goal_id = values.pop("goal_id")
        ledger = service.task_ledger(
            goal_id,
            account_scope=account_scope,
            replan_count=values.pop("replan_count", 0),
            last_replan_reason=values.pop("last_replan_reason", "") or None,
        )
        return json.dumps(
            {"ledger": ledger.to_dict(), "digest": ledger.digest()},
            indent=2,
            sort_keys=True,
        )
    if tool_name == "task_depend":
        task_id = values.pop("task_id")
        depends_on = values.pop("depends_on")
        if values.pop("remove", False):
            result = service.remove_dependency(
                task_id, depends_on, account_scope=account_scope
            )
            if not result.get("removed"):
                return "no dependency found"
            return "removed dependency: %s no longer depends on %s" % (
                result["task_id"][:8],
                result["depends_on"][:8],
            )
        result = service.add_dependency(
            task_id, depends_on, account_scope=account_scope
        )
        return "dependency added: %s depends on %s" % (
            result["task_id"][:8],
            result["depends_on"][:8],
        )
    if tool_name == "checklist_create":
        raw_items = values.pop("items_json")
        normalized = normalize_checklist_items(raw_items)
        checklist = service.create_checklist(
            values.pop("title"),
            [{"title": title, "detail": detail} for title, detail in normalized],
            account_scope=account_scope,
            **values,
        )
        return format_checklist(checklist.to_dict())
    if tool_name == "checklist_show":
        checklist = service.checklist(
            values.pop("checklist_id"), account_scope=account_scope
        )
        return format_checklist(checklist.to_dict())
    if tool_name == "checklist_update":
        checklist = service.update_checklist(
            values.pop("checklist_id"),
            values.pop("item"),
            values.pop("status"),
            account_scope=account_scope,
            **values,
        )
        return format_checklist(checklist.to_dict())
    return "unsupported scoped task tool '%s'" % tool_name


def format_unload_report(
    *,
    requested: Sequence[str],
    remaining: Sequence[str],
    residency_error: str,
    cleanup: Mapping[str, Sequence[Any]],
    errors: Sequence[str],
) -> str:
    """Format the operator-facing summary for an Ollama model unload."""
    lines = [
        "Unload requested for: %s." % (", ".join(requested) if requested else "(none)"),
    ]
    if remaining:
        lines.append("WARNING: still resident in Ollama: %s." % ", ".join(remaining))
    elif residency_error:
        lines.append("WARNING: residency could not be confirmed: %s." % residency_error)
    elif requested:
        lines.append("Ollama residency confirmed clear for the requested model(s).")
    else:
        lines.append("WARNING: no unload request was accepted by Ollama.")
    if cleanup.get("terminated"):
        lines.append(
            "Cleaned orphaned Ollama GPU-discovery probe PID(s): %s."
            % ", ".join(str(pid) for pid in cleanup["terminated"])
        )
    if cleanup.get("terminated_model_runners"):
        lines.append(
            "Cleaned verified orphaned Ollama model runner PID(s): %s."
            % ", ".join(str(pid) for pid in cleanup["terminated_model_runners"])
        )
    if cleanup.get("protected_model_runners"):
        lines.append(
            "WARNING: orphaned model runner PID(s) were not terminated automatically: %s."
            % ", ".join(str(pid) for pid in cleanup["protected_model_runners"])
        )
    for error in list(errors) + list(cleanup.get("errors") or ()):
        lines.append("WARNING: %s" % error)
    return "\n".join(lines)


def extract_session_replay_turns(events: Iterable[Any]) -> list[dict[str, str]]:
    """Pair user and assistant replay events into task/response turn dictionaries."""
    pairs: list[dict[str, str]] = []
    pending_user = ""
    for ev in events:
        kind = str(getattr(ev, "event_type", "") or getattr(ev, "kind", ""))
        payload = getattr(ev, "payload", None) or {}
        text = str(payload.get("content") or payload.get("text") or "")
        if kind == "user.message":
            if pending_user:
                pairs.append({"task": pending_user, "response": ""})
            pending_user = text
        elif kind in ("model.response", "assistant.message"):
            pairs.append({"task": pending_user, "response": text})
            pending_user = ""
    if pending_user:
        pairs.append({"task": pending_user, "response": ""})
    return pairs


def run_code_gate(
    reply: str,
    *,
    interaction_id: Any,
    regenerate: Callable[[str], Any] | None,
    allow_execution: bool,
    timeout: int,
    max_repairs: int,
    code_gate_target: Callable[[str], str | None],
    code_gate_compile_only_target: Callable[[str], str | None],
    static_errors_fn: Callable[[str], str | None],
    redact_fn: Callable[[str], str],
    run_code_detail_fn: Callable[..., Mapping[str, Any]],
    record_unavailable_fn: Callable[[str], None],
    record_failure_fn: Callable[[Any], None],
) -> tuple[str, bool | None, bool]:
    """Compile and optionally smoke-run a reply's Python block with bounded repair."""
    code = code_gate_target(reply)
    compile_only = not allow_execution
    if code is None:
        code = code_gate_compile_only_target(reply)
        if code is None:
            return reply, None, False
        compile_only = True

    if compile_only:
        static_err = static_errors_fn(code)
        if static_err is None:
            return reply, None, False
        failure = redact_fn(static_err)
    else:
        try:
            result = run_code_detail_fn(code, timeout=timeout, compile_first=True)
        except Exception as exc:
            record_unavailable_fn(type(exc).__name__)
            return reply, None, False
        if result.get("ok"):
            return reply, True, False
        if result.get("timed_out"):
            return reply, None, False
        raw_failure = str(
            result.get("stderr") or result.get("stdout") or "exited with an error"
        ).strip()
        failure = redact_fn(raw_failure) or "exited with an error"

    if regenerate is not None:
        seen_digests = {
            hashlib.sha256(code.strip().encode("utf-8", errors="replace")).hexdigest()
        }
        for _attempt in range(1, max_repairs + 1):
            safe_failure = redact_fn(failure) or "exited with an error"
            repair_prompt = (
                "The Python code block in your previous answer fails when run:\n"
                "%s\n\nReturn the corrected complete answer with a fixed, "
                "runnable Python code block." % safe_failure[:1200]
            )
            try:
                repaired = str(regenerate(repair_prompt) or "")
            except Exception:
                repaired = ""
            repaired_runnable = code_gate_target(repaired) if repaired else None
            repaired_interactive = (
                code_gate_compile_only_target(repaired)
                if (repaired and repaired_runnable is None and compile_only)
                else None
            )
            repaired_code = (
                repaired_runnable
                if repaired_runnable is not None
                else repaired_interactive
            )
            if not repaired_code:
                break
            digest = hashlib.sha256(
                repaired_code.strip().encode("utf-8", errors="replace")
            ).hexdigest()
            if digest in seen_digests:
                break
            seen_digests.add(digest)
            if repaired_runnable is not None and allow_execution:
                try:
                    retry: Mapping[str, Any] = run_code_detail_fn(
                        repaired_runnable,
                        timeout=timeout,
                        compile_first=True,
                    )
                except Exception:
                    retry = {"ok": False, "timed_out": False}
                if retry.get("ok"):
                    return repaired, True, True
                if retry.get("timed_out"):
                    failure = "%s (repair verification timed out)" % failure
                    break
                retry_raw = str(
                    retry.get("stderr") or retry.get("stdout") or ""
                ).strip()
                if retry_raw:
                    failure = redact_fn(retry_raw) or failure
            else:
                retry_static = static_errors_fn(repaired_code)
                if retry_static is None:
                    return repaired, True, True
                failure = redact_fn(retry_static) or failure

    safe_failure = redact_fn(failure) or "exited with an error"
    summary = "exited with an error"
    for line in reversed(safe_failure.splitlines()):
        if line.strip():
            summary = line.strip()
            break
    summary = redact_fn(summary) or "exited with an error"
    record_failure_fn(interaction_id)
    return (
        "%s\n\nNOT VERIFIED: the Python code block in this answer fails when "
        "run (%s)." % (reply, summary[:300]),
        False,
        False,
    )
