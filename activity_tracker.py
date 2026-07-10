"""Per-response activity tracking for tools, files, and model work.

The tracker is intentionally small and dependency-free. Server paths open a
response span, then tools and file helpers record observable events into the
current thread's span. Status endpoints can also read active spans while work is
still running.
"""
from __future__ import annotations

import contextlib
import itertools
import json
import re
import threading
import time
from copy import deepcopy


MAX_EVENTS = 80
MAX_ACTIVE = 20
MAX_EVENT_BLOCK = 4000
_LOCK = threading.RLock()
_LOCAL = threading.local()
_IDS = itertools.count(1)
_ACTIVE = {}
_LATEST = None
_TOTAL_TOOL_CALLS = 0


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _short(value, limit=220):
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit] + "..." if len(text) > limit else text


def _block(value, limit=MAX_EVENT_BLOCK):
    text = str(value or "").replace("\x00", "\\0").strip()
    return text[:limit] + "\n... (truncated)" if len(text) > limit else text


_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[-_]?key)\b"
    r"(\s*[:=]\s*|\s+)([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*")
_KEY_SHAPE_RE = re.compile(r"\b(?:sk|gh[pousr])[-_][A-Za-z0-9_-]{12,}\b")


def _redact_text(value):
    text = str(value or "")
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: "%s=<redacted>" % match.group(1), text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    return _KEY_SHAPE_RE.sub("<redacted-key>", text)


def _safe_command(value):
    if not value:
        return ""
    parsed = value
    if isinstance(value, str) and value.lstrip().startswith("["):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = value
    if not isinstance(parsed, (list, tuple)):
        return _block(_redact_text(parsed), 2400)
    rendered = []
    hide_next = False
    for item in parsed:
        text = str(item)
        lowered = text.lower().lstrip("-/")
        if hide_next:
            rendered.append("<redacted>")
            hide_next = False
            continue
        if lowered in {"password", "passwd", "token", "secret", "api-key", "api_key"}:
            rendered.append(text)
            hide_next = True
            continue
        if any(lowered.startswith(name + "=") for name in (
            "password", "passwd", "token", "secret", "api-key", "api_key",
        )):
            rendered.append(text.split("=", 1)[0] + "=<redacted>")
            continue
        rendered.append(_redact_text(text))
    return _block(json.dumps(rendered, ensure_ascii=False), 2400)


def _safe_args(value, depth=0):
    """Keep useful operation details without persisting secrets or bulk content."""
    if depth > 3:
        return "<nested>"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = str(key)
            lowered = name.lower()
            if any(part in lowered for part in ("password", "secret", "token", "approval", "api_key")):
                out[name] = "<redacted>"
            elif lowered in {"content", "code", "files_json", "stdin"}:
                out[name] = "<%d chars>" % len(str(item or ""))
            else:
                out[name] = _safe_args(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_safe_args(item, depth + 1) for item in list(value)[:64]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _block(_redact_text(value), 1000)


ACTION_TITLES = {
    "directory_tree": "Listed Folder",
    "directory_create": "Created Folder",
    "file_find": "Searched Files",
    "file_read": "Read File",
    "file_read_range": "Read File Range",
    "file_write": "Wrote File",
    "file_edit": "Edited File",
    "file_delete": "Deleted File",
    "text_search": "Searched Text",
    "script_search": "Searched Scripts",
    "program_search": "Searched Programs",
    "workspace_run": "Ran Program",
    "script_run": "Ran Script",
    "run_code": "Ran Code",
    "run_project": "Ran Project",
    "image_inspect": "Viewed Image",
    "artifact_generate": "Generated Assets",
    "artifact_verify": "Verified Assets",
    "game_reference_suite": "Ran Game Suite",
    "game_generate_and_test": "Built and Tested Game",
    "game_generation_campaign": "Ran Game Fleet",
    "ground_artifact": "Verified Artifact",
    "checklist_create": "Created Checklist",
    "checklist_update": "Updated Checklist",
    "checklist_show": "Viewed Checklist",
    "web_search": "Searched Web",
    "web_fetch": "Fetched Web Page",
}


def action_title(tool_name):
    value = str(tool_name or "tool")
    return ACTION_TITLES.get(value, value.replace("_", " ").strip().title())


def _current():
    response_id = getattr(_LOCAL, "response_id", None)
    if not response_id:
        return None
    with _LOCK:
        return _ACTIVE.get(response_id)


def _event(response, kind, **fields):
    elapsed_ms = int((time.time() - response["started_at"]) * 1000)
    event = {"ts": _now(), "elapsed_ms": elapsed_ms, "kind": kind}
    event.update({k: v for k, v in fields.items() if v not in (None, "")})
    response["events"].append(event)
    if len(response["events"]) > MAX_EVENTS:
        del response["events"][:-MAX_EVENTS]
    response["last_event"] = event
    return event


@contextlib.contextmanager
def response_span(label, prompt="", *, surface="", model="", session="", project=""):
    """Track one user-visible response.

    Nested spans reuse the outer response so helpers can be composed safely.
    """
    existing = getattr(_LOCAL, "response_id", None)
    if existing:
        yield _current()
        return
    response_id = "r%06d" % next(_IDS)
    response = {
        "id": response_id,
        "label": _short(label, 80),
        "surface": _short(surface, 80),
        "model": _short(model, 80),
        "session": _short(session, 80),
        "project": _short(project, 80),
        "prompt": _short(prompt, 500),
        "status": "running",
        "started_ts": _now(),
        "started_at": time.time(),
        "elapsed_ms": 0,
        "tool_calls": 0,
        "model_calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "file_creates": 0,
        "file_edits": 0,
        "file_deletes": 0,
        "lines_added": 0,
        "lines_edited": 0,
        "lines_deleted": 0,
        "files": [],
        "checklist": None,
        "result_summary": "",
        "events": [],
        "last_event": None,
    }
    with _LOCK:
        _ACTIVE[response_id] = response
        if len(_ACTIVE) > MAX_ACTIVE:
            oldest = sorted(_ACTIVE, key=lambda k: _ACTIVE[k]["started_at"])[:1]
            for key in oldest:
                if key != response_id:
                    _ACTIVE.pop(key, None)
    _LOCAL.response_id = response_id
    try:
        record_event("response_start", summary=_short(prompt, 180))
        yield response
        response["status"] = "complete"
    except Exception as exc:
        response["status"] = "error"
        record_event("response_error", summary=_short(exc, 180))
        raise
    finally:
        response["elapsed_ms"] = int((time.time() - response["started_at"]) * 1000)
        if response["status"] == "complete":
            record_event("response_complete", summary="%d tool call(s)" % response["tool_calls"])
        with _LOCK:
            global _LATEST
            _ACTIVE.pop(response_id, None)
            _LATEST = deepcopy(response)
        _LOCAL.response_id = None


def record_event(kind, **fields):
    response = _current()
    if response is None:
        return None
    with _LOCK:
        return deepcopy(_event(response, kind, **fields))


def record_model_call(
    model="", prompt_chars=0, history_messages=0, ok=True, elapsed_ms=0,
    tokens_in=0, tokens_out=0, token_source="",
):
    response = _current()
    if response is None:
        return
    with _LOCK:
        response["model_calls"] += 1
        response["tokens_in"] += int(tokens_in or 0)
        response["tokens_out"] += int(tokens_out or 0)
        _event(
            response,
            "model_call",
            model=_short(model, 80),
            prompt_chars=int(prompt_chars or 0),
            history_messages=int(history_messages or 0),
            tokens_in=int(tokens_in or 0),
            tokens_out=int(tokens_out or 0),
            token_source=_short(token_source, 40),
            ok=bool(ok),
            elapsed_ms=int(elapsed_ms or 0),
        )


def record_tool_call(name, args=None, *, ok=True, elapsed_ms=0, summary=""):
    response = _current()
    if response is None:
        return
    with _LOCK:
        global _TOTAL_TOOL_CALLS
        response["tool_calls"] += 1
        _TOTAL_TOOL_CALLS += 1
        _event(
            response,
            "tool_call",
            tool=_short(name, 80),
            title=action_title(name),
            args=_safe_args(args or {}),
            ok=bool(ok),
            elapsed_ms=int(elapsed_ms or 0),
            summary=_short(_redact_text(summary), 220),
        )


def record_tool_result(
    name, args=None, *, ok=True, elapsed_ms=0, summary="", command="", output="",
):
    """Record a replay-friendly bounded tool event."""
    response = _current()
    if response is None:
        return
    with _LOCK:
        global _TOTAL_TOOL_CALLS
        response["tool_calls"] += 1
        _TOTAL_TOOL_CALLS += 1
        _event(
            response,
            "tool_call",
            tool=_short(name, 80),
            title=action_title(name),
            args=_safe_args(args or {}),
            command=_safe_command(command),
            output=_block(_redact_text(output), MAX_EVENT_BLOCK),
            ok=bool(ok),
            elapsed_ms=int(elapsed_ms or 0),
            summary=_short(_redact_text(summary), 220),
        )


def set_checklist(checklist):
    response = _current()
    if response is None:
        return
    with _LOCK:
        response["checklist"] = deepcopy(checklist) if checklist else None
        _event(
            response,
            "checklist",
            title="Updated Checklist",
            summary=_short((checklist or {}).get("summary", ""), 220),
        )


def set_result_summary(summary):
    response = _current()
    if response is None:
        return
    with _LOCK:
        response["result_summary"] = _block(summary, 1000)


@contextlib.contextmanager
def tool_dispatch_context():
    depth = int(getattr(_LOCAL, "tool_depth", 0) or 0)
    _LOCAL.tool_depth = depth + 1
    try:
        yield
    finally:
        _LOCAL.tool_depth = depth


def inside_tool_call():
    return int(getattr(_LOCAL, "tool_depth", 0) or 0) > 0


def record_file_change(
    action,
    path,
    *,
    lines_added=0,
    lines_edited=0,
    lines_deleted=0,
    bytes_written=0,
    dry_run=False,
    summary="",
):
    response = _current()
    if response is None:
        return
    action = (action or "").lower()
    item = {
        "action": action,
        "path": str(path or ""),
        "lines_added": int(lines_added or 0),
        "lines_edited": int(lines_edited or 0),
        "lines_deleted": int(lines_deleted or 0),
        "bytes": int(bytes_written or 0),
        "dry_run": bool(dry_run),
        "summary": _short(summary, 160),
    }
    with _LOCK:
        if not item["dry_run"]:
            if "create" in action:
                response["file_creates"] += 1
            elif "delete" in action:
                response["file_deletes"] += 1
            else:
                response["file_edits"] += 1
            response["lines_added"] += item["lines_added"]
            response["lines_edited"] += item["lines_edited"]
            response["lines_deleted"] += item["lines_deleted"]
        response["files"].append(item)
        if len(response["files"]) > 30:
            del response["files"][:-30]
        _event(response, "file_change", **item)


def snapshot():
    with _LOCK:
        active = [deepcopy(v) for v in sorted(_ACTIVE.values(), key=lambda r: r["started_at"])]
        now = time.time()
        for row in active:
            row["elapsed_ms"] = int((now - row.get("started_at", now)) * 1000)
        return {
            "active_count": len(active),
            "active": active,
            "latest": deepcopy(_LATEST),
            "total_tool_calls": _TOTAL_TOOL_CALLS,
        }


def latest():
    with _LOCK:
        return deepcopy(_LATEST)


def current():
    response = _current()
    return deepcopy(response) if response else None


def _args_text(args):
    if not args:
        return ""
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(args)


def format_transcript(response=None, max_actions=30):
    response = response or _current() or latest()
    if not response:
        return "(no actions yet)"
    actions = [
        event for event in (response.get("events") or [])
        if event.get("kind") == "tool_call"
    ][-_bounded_action_count(max_actions):]
    if not actions:
        return "(no tool actions)"
    lines = []
    for event in actions:
        marker = "•" if event.get("ok", True) else "×"
        lines.append("%s %s" % (marker, event.get("title") or action_title(event.get("tool"))))
        command = event.get("command") or ""
        args_text = _args_text(event.get("args"))
        output = event.get("output") or event.get("summary") or ""
        detail = command or args_text
        if detail:
            detail_lines = detail.splitlines() or [detail]
            for line in detail_lines[:-1]:
                lines.append("  │ %s" % line)
            lines.append("  │ %s" % detail_lines[-1])
        if output:
            output_lines = output.splitlines() or [output]
            if len(output_lines) > 12:
                output_lines = output_lines[:10] + ["... (%d more lines)" % (len(output_lines) - 10)]
            for line in output_lines[:-1]:
                lines.append("  │ %s" % line)
            lines.append("  └ %s" % output_lines[-1])
        elif detail:
            lines[-1] = lines[-1].replace("  │ ", "  └ ", 1)
    return "\n".join(lines)


def _bounded_action_count(value):
    try:
        return max(1, min(80, int(value or 30)))
    except (TypeError, ValueError):
        return 30


def format_end_report(response=None):
    response = response or _current() or latest()
    if not response:
        return "=== END REPORT ===\nresult: unavailable"
    checklist = response.get("checklist") or {}
    items = checklist.get("items") or []
    done = sum(1 for item in items if item.get("status") == "done")
    lines = [
        "=== END REPORT ===",
        "result: %s" % response.get("status", "unknown"),
        "elapsed: %sms | model calls: %s | tool calls: %s" % (
            response.get("elapsed_ms", 0), response.get("model_calls", 0),
            response.get("tool_calls", 0),
        ),
        "files: +%s ~%s -%s | lines: +%s ~%s -%s" % (
            response.get("file_creates", 0), response.get("file_edits", 0),
            response.get("file_deletes", 0), response.get("lines_added", 0),
            response.get("lines_edited", 0), response.get("lines_deleted", 0),
        ),
    ]
    if items:
        lines.append("checklist: %d/%d complete" % (done, len(items)))
        symbols = {"done": "[x]", "in_progress": "[~]", "blocked": "[!]"}
        for item in items:
            lines.append("  %s %s" % (symbols.get(item.get("status"), "[ ]"), item.get("title", "")))
    if response.get("result_summary"):
        lines.append("summary: %s" % response["result_summary"])
    files = response.get("files") or []
    if files:
        lines.append("changed paths:")
        for item in files[-12:]:
            lines.append("  - %s %s" % (item.get("action", "file"), item.get("path", "")))
    return "\n".join(lines)


def format_response(response=None):
    response = response or _current() or latest()
    if not response:
        return "activity: none yet"
    if response.get("status") == "running" and response.get("started_at"):
        response = deepcopy(response)
        response["elapsed_ms"] = int((time.time() - response["started_at"]) * 1000)
    lines = [
        "=== ACTIVITY (observable work) ===",
        "response: %(id)s %(status)s %(label)s in %(elapsed_ms)sms" % response,
        "model calls: %s   tool calls: %s   tokens in/out: %s/%s" % (
            response.get("model_calls", 0),
            response.get("tool_calls", 0),
            response.get("tokens_in", 0),
            response.get("tokens_out", 0),
        ),
        "files: +%s ~%s -%s   lines: +%s ~%s -%s" % (
            response.get("file_creates", 0),
            response.get("file_edits", 0),
            response.get("file_deletes", 0),
            response.get("lines_added", 0),
            response.get("lines_edited", 0),
            response.get("lines_deleted", 0),
        ),
    ]
    files = response.get("files") or []
    if files:
        lines.append("file changes:")
        for item in files[-8:]:
            suffix = " dry-run" if item.get("dry_run") else ""
            lines.append(
                "  %(action)s %(path)s  lines +%(lines_added)s ~%(lines_edited)s -%(lines_deleted)s%(suffix)s"
                % {**item, "suffix": suffix}
            )
    events = response.get("events") or []
    if events:
        lines.append("recent events:")
        for event in events[-10:]:
            detail = event.get("summary") or event.get("tool") or event.get("path") or event.get("model") or ""
            lines.append("  +%sms %s %s" % (event.get("elapsed_ms", 0), event.get("kind", ""), detail))
    transcript = format_transcript(response)
    if transcript != "(no tool actions)":
        lines.extend(["actions:", transcript])
    lines.append("=== END ACTIVITY ===")
    return "\n".join(lines)


def reset_for_tests():
    with _LOCK:
        global _LATEST, _TOTAL_TOOL_CALLS
        _ACTIVE.clear()
        _LATEST = None
        _TOTAL_TOOL_CALLS = 0
    _LOCAL.response_id = None
