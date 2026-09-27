"""REPL presentation for the developer tools: ``/tools``, ``/test``, ``/digest``.

Root-free and adapter-free: every function receives the composed
``DeveloperToolServices`` aggregate (or None when this runtime did not compose
it) plus an ``OperationContext`` built by the REPL. Values are read from the
service results by attribute, so this module depends only on the application
layer. The local operator is shown full paths; nothing here is model-visible.
"""
from __future__ import annotations

import re
from typing import Any

from ....application.context import OperationContext
from ....application.diagnostics.service import render_output_digest
from ....application.errors import (
    CapacityExceeded,
    InvalidInput,
    NotFound,
    SonderError,
)


NOT_COMPOSED = "developer tools are not composed in this runtime"
TOOLS_USAGE = "usage: /tools [refresh [full] | <category> | <tool name>]"
TEST_USAGE = (
    "usage: /test [runner|auto] [selector]  |  /test status|result|cancel <job_id>"
)
DIGEST_USAGE = "usage: /digest <job_id | path>"
TEST_RUNNERS = (
    "auto", "pytest", "unittest", "ctest", "cargo", "go", "dotnet", "npm",
    "pnpm", "yarn", "gradle", "maven", "make",
)
TEST_ACTIONS = ("status", "result", "cancel")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_TERMINAL_STATUSES = frozenset({
    "passed", "failed", "error", "no_tests", "cancelled", "timed_out",
})
_RENDER_MAX_CHARS = 12_000
_DIGEST_MAX_CHARS = 8_000


def _text(value: Any) -> str:
    return str(getattr(value, "value", value) if value is not None else "")


def _code(exc: BaseException) -> str:
    return str(getattr(exc, "code", "") or type(exc).__name__)


def _age(seconds: Any) -> str:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return "?"
    if value < 120:
        return "%ds" % value
    if value < 7200:
        return "%dm" % (value // 60)
    if value < 172_800:
        return "%dh" % (value // 3600)
    return "%dd" % (value // 86_400)


# --- /tools --------------------------------------------------------------------


def _render_inventory(view: Any, *, detailed: bool) -> str:
    tools = tuple(getattr(view, "tools", ()) or ())
    header = "host tools: %d on %s %s; snapshot %s, age %s" % (
        len(tools), _text(getattr(view, "os", "")) or "?",
        _text(getattr(view, "machine", "")), _text(getattr(view, "snapshot_digest", ""))[:12],
        _age(getattr(view, "age_seconds", None)),
    )
    if getattr(view, "stale", False):
        header += " (stale -- /tools refresh)"
    lines = [header]
    filtered = _text(getattr(view, "filtered_by", ""))
    if filtered:
        lines.append("filter: %s" % filtered)
    if not tools:
        lines.append("  (no matching tools)")
        return "\n".join(lines)
    current = None
    for tool in tools:
        category = _text(getattr(tool, "category", ""))
        if category != current:
            current = category
            count = sum(1 for item in tools if _text(getattr(item, "category", "")) == category)
            lines.append("%s (%d):" % (category or "other", count))
        version = _text(getattr(tool, "version", "")) or "(%s)" % _text(
            getattr(tool, "version_status", "")
        )
        path = _text(getattr(tool, "path_display", ""))
        marker = "" if getattr(tool, "on_path", True) else "  [%s]" % _text(
            getattr(tool, "source", "")
        )
        lines.append("  %-18s %-18s %s%s" % (
            _text(getattr(tool, "name", "")), version[:18], path, marker,
        ))
        if detailed:
            for alternative in tuple(getattr(tool, "alternatives_display", ()) or ())[:4]:
                lines.append("      also: %s" % alternative)
            for key, value in tuple(getattr(tool, "details", ()) or ())[:8]:
                lines.append("      %s: %s" % (key, value))
    text = "\n".join(lines)
    if len(text) > _RENDER_MAX_CHARS:
        text = text[:_RENDER_MAX_CHARS].rstrip() + "\n... (cut; filter by category)"
    return text


def _categories(inventory: Any) -> str:
    try:
        view = inventory.view(redacted=False)
    except Exception:
        return ""
    return ", ".join(name for name, _ in tuple(getattr(view, "counts", ()) or ()))


def render_tools_command(services: Any, arg: str) -> str:
    """``/tools``, ``/tools refresh [full]``, ``/tools <category>``, ``/tools <name>``."""
    inventory = getattr(services, "inventory", None) if services is not None else None
    if inventory is None:
        return NOT_COMPOSED
    words = str(arg or "").split()
    try:
        if not words:
            return _render_inventory(inventory.view(redacted=False), detailed=False)
        if words[0].lower() == "refresh":
            if words[1:] not in ([], ["full"]):
                return TOOLS_USAGE
            inventory.snapshot(refresh=True, full=words[1:] == ["full"])
            return _render_inventory(inventory.view(redacted=False), detailed=False)
        if len(words) != 1:
            return TOOLS_USAGE
        token = words[0]
        try:
            view = inventory.view(category=token.lower(), redacted=False)
            return _render_inventory(view, detailed=False)
        except InvalidInput:
            pass
        try:
            view = inventory.view(name=token, redacted=False)
        except InvalidInput:
            view = None
        if view is None or not tuple(getattr(view, "tools", ()) or ()):
            known = _categories(inventory)
            return "unknown category or tool: %s%s" % (
                token[:64], ("\ncategories: %s" % known) if known else "",
            )
        return _render_inventory(view, detailed=True)
    except SonderError as exc:
        return "tool inventory unavailable: %s" % _code(exc)


# --- /test ---------------------------------------------------------------------


def _parse_test_arg(arg: str) -> tuple[str, str]:
    text = str(arg or "").strip()
    if not text:
        return "auto", ""
    first, _, rest = text.partition(" ")
    if first.lower() in TEST_RUNNERS:
        return first.lower(), rest.strip()
    return "auto", text


def _status_line(view: Any) -> str:
    job_id = _text(getattr(view, "job_id", ""))
    line = "test run %s: %s (%s)" % (
        job_id, _text(getattr(view, "status", "")) or "?",
        _text(getattr(view, "runner", "")) or "?",
    )
    elapsed = getattr(view, "elapsed_seconds", None)
    if isinstance(elapsed, (int, float)):
        line += " %.0fs" % elapsed
    command = tuple(getattr(view, "display_command", ()) or ())
    if command:
        line += "\n  command: %s" % " ".join(str(part) for part in command)
    return line


def _render_report(report: Any) -> str:
    lines = []
    header = "test run %s: %s (%s)" % (
        _text(getattr(report, "job_id", "")), _text(getattr(report, "status", "")),
        _text(getattr(report, "runner", "")),
    )
    exit_code = getattr(report, "exit_code", None)
    if exit_code is not None:
        header += " exit=%s" % exit_code
    duration = getattr(report, "duration_seconds", None)
    if isinstance(duration, (int, float)):
        header += " in %.1fs" % duration
    lines.append(header)
    command = tuple(getattr(report, "display_command", ()) or ())
    if command:
        lines.append("  command: %s" % " ".join(str(part) for part in command))
    totals = getattr(report, "totals", None)
    if totals is not None:
        parts = ["%s=%s" % (name, getattr(totals, name, "?"))
                 for name in ("passed", "failed", "skipped", "errors", "total")]
        source = _text(getattr(report, "totals_source", ""))
        reliable = getattr(report, "totals_reliable", True)
        lines.append("  totals: %s%s%s" % (
            " ".join(parts), (" (from %s)" % source) if source else "",
            "" if reliable else " [unreliable: sources disagree or text-only]",
        ))
    summary = _text(getattr(report, "summary_line", ""))
    if summary:
        lines.append("  summary: %s" % summary)
    failures = tuple(getattr(report, "failures", ()) or ())
    if failures:
        lines.append("  failures:")
        for failure in failures:
            location = _text(getattr(failure, "file", ""))
            line_no = getattr(failure, "line", None)
            if location and line_no is not None:
                location += ":%s" % line_no
            lines.append("    %s%s %s" % (
                _text(getattr(failure, "id", "")),
                (" (%s)" % location) if location else "",
                _text(getattr(failure, "message_excerpt", "")),
            ))
        if getattr(report, "failures_truncated", False):
            lines.append("    ... more failures not shown")
    for note in tuple(getattr(report, "notes", ()) or ()):
        lines.append("  note: %s" % note)
    if getattr(report, "output_truncated", False) or getattr(report, "report_truncated", False):
        lines.append("  (output or report truncated)")
    text = "\n".join(lines)
    return text[:_RENDER_MAX_CHARS]


def _is_report(value: Any) -> bool:
    return hasattr(value, "totals") and hasattr(value, "failures")


def start_test_command(
    services: Any, arg: str, context: OperationContext, *, project: str = ".",
) -> tuple[str, str | None]:
    """Start a structured test run; returns ``(text, job_id)``."""
    runs = getattr(services, "test_runs", None) if services is not None else None
    if runs is None:
        return NOT_COMPOSED, None
    try:
        from ....application.testing.ports import TestRunRequest
    except ImportError:
        return NOT_COMPOSED, None
    runner, selector = _parse_test_arg(arg)
    try:
        job_id = runs.start(
            TestRunRequest(project=project or ".", runner=runner, selector=selector),
            context,
        )
    except CapacityExceeded as exc:
        return "test run refused: %s (wait for a running test job)" % _code(exc), None
    except InvalidInput as exc:
        return "test run refused: %s: %s" % (_code(exc), str(exc)[:300]), None
    except (SonderError, PermissionError) as exc:
        return "test run refused: %s" % _code(exc), None
    try:
        return "started " + _status_line(runs.status(job_id, context)), job_id
    except SonderError:
        return "started test run %s (%s)" % (job_id, runner), job_id


def poll_test_result(
    services: Any, job_id: str, context: OperationContext, *, wait_seconds: float = 1.0,
) -> tuple[str, bool]:
    """``(text, finished)`` after waiting at most ``wait_seconds`` for the job."""
    runs = getattr(services, "test_runs", None) if services is not None else None
    if runs is None:
        return NOT_COMPOSED, True
    try:
        value = runs.result(job_id, context, wait_seconds=max(0.0, float(wait_seconds)))
    except NotFound:
        return "no test run job %s" % job_id, True
    except SonderError as exc:
        return "test run result unavailable: %s" % _code(exc), True
    if _is_report(value):
        return _render_report(value), True
    finished = _text(getattr(value, "status", "")) in _TERMINAL_STATUSES
    return _status_line(value), finished


def render_test_followup(
    services: Any,
    action: str,
    job_id: str,
    context: OperationContext,
    *,
    wait_seconds: float = 0,
) -> str:
    """``/test status|result|cancel <job_id>``."""
    runs = getattr(services, "test_runs", None) if services is not None else None
    if runs is None:
        return NOT_COMPOSED
    verb = str(action or "").strip().lower()
    identifier = str(job_id or "").strip()
    if verb not in TEST_ACTIONS or not _JOB_ID_RE.match(identifier):
        return TEST_USAGE
    if verb == "result":
        return poll_test_result(services, identifier, context, wait_seconds=wait_seconds)[0]
    try:
        if verb == "status":
            return _status_line(runs.status(identifier, context))
        view = runs.cancel(identifier, context, reason="cancelled by the local operator")
        return "cancelled " + _status_line(view)
    except NotFound:
        return "no test run job %s" % identifier
    except SonderError as exc:
        return "test run %s failed: %s" % (verb, _code(exc))


# --- /digest -------------------------------------------------------------------


def render_digest_command(services: Any, arg: str, context: OperationContext) -> str:
    """``/digest <job_id|path>``: an existing job wins, otherwise a guarded file."""
    digest = getattr(services, "digest", None) if services is not None else None
    if digest is None:
        return NOT_COMPOSED
    target = str(arg or "").strip()
    if not target:
        return DIGEST_USAGE
    if _JOB_ID_RE.match(target):
        try:
            result = digest.digest_job(target, context, operator=True)
            return render_output_digest(result, max_chars=_DIGEST_MAX_CHARS)
        except NotFound:
            pass
        except SonderError as exc:
            if _code(exc) not in ("DEPENDENCY_UNAVAILABLE",):
                return "digest failed: %s" % _code(exc)
    try:
        result = digest.digest_file(target, context)
    except PermissionError:
        return (
            "digest refused: %s is outside the guarded digest surface "
            "(allowed roots only; credential stores and secret files are refused)"
            % target[:300]
        )
    except SonderError as exc:
        return "digest failed: %s" % _code(exc)
    return render_output_digest(result, max_chars=_DIGEST_MAX_CHARS)


__all__ = [
    "DIGEST_USAGE", "NOT_COMPOSED", "TEST_RUNNERS", "TEST_USAGE", "TOOLS_USAGE",
    "poll_test_result", "render_digest_command", "render_test_followup",
    "render_tools_command", "start_test_command",
]
