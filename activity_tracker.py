"""Per-response activity tracking for tools, files, and model work.

The tracker is intentionally small and dependency-free. Server paths open a
response span, then tools and file helpers record observable events into the
current thread's span. Status endpoints can also read active spans while work is
still running.
"""
from __future__ import annotations

import contextlib
import itertools
import threading
import time
from copy import deepcopy


MAX_EVENTS = 80
MAX_ACTIVE = 20
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
        "file_creates": 0,
        "file_edits": 0,
        "file_deletes": 0,
        "lines_added": 0,
        "lines_edited": 0,
        "lines_deleted": 0,
        "files": [],
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


def record_model_call(model="", prompt_chars=0, history_messages=0, ok=True, elapsed_ms=0):
    response = _current()
    if response is None:
        return
    with _LOCK:
        response["model_calls"] += 1
        _event(
            response,
            "model_call",
            model=_short(model, 80),
            prompt_chars=int(prompt_chars or 0),
            history_messages=int(history_messages or 0),
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
            args=_short(args, 260),
            ok=bool(ok),
            elapsed_ms=int(elapsed_ms or 0),
            summary=_short(summary, 220),
        )


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
        "model calls: %s   tool calls: %s" % (
            response.get("model_calls", 0),
            response.get("tool_calls", 0),
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
    lines.append("=== END ACTIVITY ===")
    return "\n".join(lines)


def reset_for_tests():
    with _LOCK:
        global _LATEST, _TOTAL_TOOL_CALLS
        _ACTIVE.clear()
        _LATEST = None
        _TOTAL_TOOL_CALLS = 0
    _LOCAL.response_id = None
