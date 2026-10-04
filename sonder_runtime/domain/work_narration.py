"""Deterministic narration projected from existing host decisions and events.

No inference or retained state. Adapters supply authorized snapshots; activity
must already have passed its normal redaction/detail projection.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime
from typing import Any, Iterable

_TERMINAL = frozenset({"done", "finished", "completed", "passed", "success", "succeeded", "failed", "error", "cancelled", "canceled", "blocked", "interrupted", "task_drift", "paused"})
_FAILED = frozenset({"failed", "error", "interrupted", "task_drift", "blocked"})
_URGENT = frozenset({"task_pass", "task_fail", "task_finished", "validation_failed", "failed", "error", "completed", "cancelled", "interrupted", "blocked", "paused", "final", "summary"})
_NOISE_EVENTS = frozenset({
    "reasoning", "model_token", "token", "response_start", "model_call",
    "model_start", "model_end", "generation", "generation_start",
    "generation_end", "inference", "inference_start", "inference_end",
})


# Credential assignments (`DB_PASSWORD=...`, `api-key: ...`, `pwd2=...`) are
# found by walking maximal identifier runs once and testing each run's name,
# not by one regex that both locates the name and requires `=` after it. That
# regex failed only after scanning to the end of a run, and was then retried
# from every position inside it: quadratic on a long run of '-' or repeated
# 'pwd' (CodeQL py/polynomial-redos). The value is matched only after a
# credential name, so a non-credential assignment never swallows one nested
# inside its value (`x=password=secret`).
_NAME = re.compile(r"(?i)[A-Z0-9_-]+")
_CREDENTIAL_NAME = re.compile(r"(?i)password|passwd|pwd|token|secret|api[-_]?key|credential")
# Separator, then value. Whitespace before the separator is skipped in Python
# (str.isspace is the test re's \s applies), so the pattern starts with a fixed
# character rather than a repetition (CodeQL py/polynomial-redos).
_ASSIGNED_VALUE = re.compile(r"[=:]\s*([^\s,;]+)")
# A JWT is three dot-joined runs. The dotted tail is optional so a run that is
# not one still matches, and is kept, instead of failing and being rescanned
# from each later 'eyJ' inside it.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)?)?")


def _redact_assignments(text: str) -> str:
    parts, cursor = [], 0
    for name in _NAME.finditer(text):
        if name.start() < cursor or not _CREDENTIAL_NAME.search(name.group()):
            continue
        start = name.end()
        while start < len(text) and text[start].isspace():
            start += 1
        value = _ASSIGNED_VALUE.match(text, start)
        if value is None:
            continue
        parts.append(text[cursor:value.start(1)])
        parts.append("<redacted>")
        cursor = value.end(1)
    parts.append(text[cursor:])
    return "".join(parts)


def _text(value: Any, limit: int = 240) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    text = re.sub(r"[\x00-\x20\x7f]+", " ", str(value)).strip()
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1<redacted>", text)
    text = _redact_assignments(text)
    text = re.sub(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@", r"\1<redacted>@", text)
    text = _JWT.sub(lambda m: "<redacted-token>" if m.group(1) else m.group(0), text)
    return text[:limit]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else default
    except (TypeError, ValueError, OverflowError):
        return default


def _epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return _number(value)
    try:
        parsed = datetime.fromisoformat(_text(value, 80).replace("Z", "+00:00"))
        # Legacy activity timestamps are host-local, without an offset.
        # datetime.timestamp preserves that convention; explicit offsets
        # from other event sources still describe their original instant.
        return max(0, parsed.timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, (list, tuple)) else []


def _event(run_id, text, *, kind="update", at=0, final=False, source=""):
    text = _text(text, 600)
    stamp = _epoch(at)
    # Source ids survive sliding event windows. Snapshot records use their
    # authoritative update timestamp and text as the source identity.
    identity = str(source or "%s|%.6f|%s" % (kind, stamp, text))
    digest = hashlib.sha256((str(run_id) + "|" + kind + "|" + identity).encode()).hexdigest()[:20]
    return {"id": "progress-" + digest, "run_id": str(run_id), "text": text,
            "kind": kind, "at": stamp, "final": bool(final)}


def _rate_limit(rows, limit=24):
    # Stable sort preserves source ordering when timestamps coincide.
    rows.sort(key=lambda row: row["at"])
    kept, seen, last = [], set(), {}
    for row in rows:
        if row["id"] in seen or not row["text"]:
            continue
        seen.add(row["id"])
        rid = row["run_id"]
        urgent = row["final"] or row["kind"] in _URGENT
        if not urgent and rid in last and row["at"] - last[rid] < 10:
            continue
        kept.append(row)
        # Finishes and failures bypass throttling, then restart its interval.
        last[rid] = row["at"]
    return kept[-limit:] if limit > 0 else []


def acknowledgement(goal: Any, mode: Any, project: Any = "", tier: Any = "",
                    host: Any = "", reason: Any = "", run_id: Any = "",
                    agents: Any = None, worker_slots: Any = None,
                    per_agent_seconds: Any = None, tasks: Iterable[Any] = (),
                    watch: Any = "", cancel: Any = "") -> str:
    """A pre-execution plan from known admission facts, never an inferred ETA."""
    mode = _text(mode, 40).lower()
    rid = _text(run_id, 80)
    count, slots = int(_number(agents)), int(_number(worker_slots))
    if mode in {"fleet", "master_orchestrate"}:
        how = "a fleet of %d agents on %d worker slots" % (count, slots) if count and slots else "a fleet"
        controls = ("/agents", "/agentcancel " + (rid or "<master-id>"))
    elif mode == "autopilot":
        how = "autopilot"
        controls = ("/autopilot status " + rid, "/autopilot cancel " + (rid or "<run-id>"))
    elif mode in {"fanout", "model_fanout"}:
        how = "model fanout"
        controls = ("model_fanout_status('%s')" % (rid or "<run-id>"), "model_fanout_cancel('%s')" % (rid or "<run-id>"))
    elif mode == "decide":
        how = "automatic work routing; the next update will name the selected lane"
        controls = ("the live conversation", "Ctrl+C")
    else:
        how = "a single agent"
        controls = ("the live conversation", "Ctrl+C")
    first = [_text(task.get("title") or task.get("description"), 90) if isinstance(task, dict)
             else _text(task, 90) for task in list(tasks or ())[:3]]
    if any(first):
        how += ", starting with " + "; ".join(task for task in first if task)
    elif mode == "autopilot":
        how += ", first creating the task plan"
    parts = ["I will work on: %s. Mode: %s." % (_text(goal, 180) or "the requested goal", how)]
    parts.append("Where: %s; tier %s; host %s." % (_text(project, 180) or "no project folder selected",
                 _text(tier, 60) or "not selected yet", _text(host, 100) or "not selected yet"))
    parts.append("Why: %s." % (_text(reason, 180) or "the requested execution mode"))
    seconds = _number(per_agent_seconds)
    if seconds and count and slots:
        parts.append("Estimate: about %ds from the per-agent estimate." % (math.ceil(count / slots) * seconds))
    if rid.startswith("wr-"):
        controls = ("GET /v1/work-runs/" + rid, "POST /v1/work-runs/" + rid + "/cancel")
    parts.append("watch %s; cancel with %s." % (_text(watch, 180) or controls[0], _text(cancel, 180) or controls[1]))
    return " ".join(parts)


def _validation(tasks):
    attempted, passed, failed = 0, 0, 0
    for task in _rows(tasks):
        receipt = task.get("host_receipt")
        if not isinstance(receipt, dict) or receipt.get("validation_attempted") is not True:
            continue
        attempted += 1
        if receipt.get("validation_passed") is True:
            passed += 1
        elif receipt.get("validation_passed") is False:
            failed += 1
    if not attempted:
        return "Validation: no validation receipt recorded."
    return "Validation: %d attempted, %d passed, %d failed%s." % (
        attempted, passed, failed, ", %d unknown" % (attempted - passed - failed) if attempted != passed + failed else "")


def _receipt_text(value: Any) -> str:
    """Return a small receipt reference without treating prose as validation."""
    if isinstance(value, str):
        return _text(value, 160)
    if isinstance(value, dict):
        for key in ("id", "receipt_id", "summary", "artifact", "path"):
            text = _text(value.get(key), 160)
            if text:
                return text
    return ""


def _location(record: dict, fallback: str = "") -> str:
    for key in ("project", "output_folder", "output_path", "artifact", "path"):
        value = _text(record.get(key), 160)
        if value:
            return value
    return _text(fallback, 160)


def fleet_progress(snapshot: Any, run_id=""):
    data = snapshot if isinstance(snapshot, dict) else {}
    agents = _rows(data.get("agents"))
    roots = [row for row in agents if not row.get("parent_id") and (not run_id or row.get("id") == run_id)]
    if run_id and not roots:
        candidate = data.get("master") or data.get("latest_master")
        if isinstance(candidate, dict) and str(candidate.get("id", "")) == str(run_id):
            roots = [candidate]
        elif any(str(row.get("parent_id", "")) == str(run_id) for row in agents):
            roots = [{"id": str(run_id), "requested_agents": data.get("requested_agents", 0),
                      "worker_slots": data.get("worker_slots", data.get("worker_slots_total", 0))}]
    result = []
    for root in roots:
        rid = str(root.get("id", ""))
        members = [row for row in agents if row.get("parent_id") == rid and row.get("role") not in {"master", "audit"}]
        identities = {rid, *(str(row.get("id")) for row in members)}
        for event in _rows(data.get("events")):
            if str(event.get("agent_id")) not in identities:
                continue
            message = _text(event.get("message"))
            kind = "task_finished" if re.match(r"^(done|completed|finished)\b", message, re.I) else "task_started"
            if re.search(r"^(failed|error|cancelled|interrupted|task_drift)\b", message, re.I):
                kind = "failed"
            stamp = event.get("at", event.get("ts"))
            # Fleet's legacy stamp is HH:MM:SS. Events have a durable event_id
            # and recorded_ts on new snapshots; old stamps are ordered but no
            # elapsed-time or ETA claim is made from those strings.
            result.append(_event(rid, message, kind=kind, at=event.get("recorded_ts", stamp),
                final=kind in {"task_finished", "failed"}, source=event.get("event_id") or "%s|%s|%s" % (event.get("agent_id"), stamp, message)))
        done = sum(row.get("status") in _TERMINAL for row in members)
        running = sum(row.get("status") == "running" for row in members)
        queued = sum(row.get("status") == "queued" for row in members)
        declared = int(_number(root.get("requested_agents")))
        total = max(declared, len(members))
        listed = len(members) >= total
        samples = [_epoch(row.get("finished_ts")) - _epoch(row.get("started_ts")) for row in members
                   if row.get("status") in {"done", "finished", "completed"}
                   and _epoch(row.get("started_ts")) and _epoch(row.get("finished_ts"))]
        samples = [sample for sample in samples if sample > 0]
        eta = ""
        slots = int(_number(root.get("worker_slots") or data.get("worker_slots")))
        if samples and slots and listed and done < total:
            eta = "; estimate about %ds remaining" % math.ceil((total - done) / slots * sum(samples) / len(samples))
        if total:
            fleet_text = "%s%d/%d done, %d running, %d queued%s." % (
                "Fleet: " if listed else "Fleet (listed tasks): ", done, total, running, queued, eta)
            result.append(_event(rid, fleet_text, kind="fleet",
                at=max((_epoch(row.get("updated_ts")) for row in [root, *members]), default=0),
                source="%s:fleet:%s:%s:%s:%s:%s" % (rid, int(listed), done, total, running, queued)))
        for row in members:
            status = row.get("status")
            if status not in _TERMINAL:
                continue
            message = _text(row.get("error") or row.get("summary") or row.get("activity") or status)
            result.append(_event(rid, "Task %s %s: %s" % (row.get("id", ""), status, message),
                kind="failed" if status in _FAILED else "task_finished", final=True,
                at=row.get("finished_ts") or row.get("updated_ts"), source=str(row.get("id")) + ":" + str(status)))
        if root.get("status") in _TERMINAL:
            failed = sum(row.get("status") in _FAILED for row in members)
            output = _text(root.get("summary") or root.get("output"), 200)
            receipt = _receipt_text(root.get("host_receipt"))
            evidence = (" Receipt: " + receipt + ".") if receipt else ""
            text = "Fleet %s. Result: %s. Where: %s. %s%s Tasks failed: %d%s." % (
                root["status"], output or "no output recorded", _location(root) or "no project folder recorded",
                _validation(members), evidence, failed, " (listed tasks)" if not listed else "")
            if root.get("error"):
                text += " Reason: " + _text(root["error"])
            final_at = root.get("finished_ts") or root.get("updated_ts")
            result.append(_event(rid, text, kind="final", final=True, at=final_at,
                                 source=rid + ":" + root["status"] + ":" + str(final_at or "")))
    return _rate_limit(result, 1000)


def autopilot_progress(snapshot: Any, run_id=""):
    data = snapshot if isinstance(snapshot, dict) else {}
    runs = _rows(data.get("runs")) or _rows([data.get("run") or data.get("latest")])
    if not runs and data.get("id"):
        runs = [data]
    result = []
    for run in runs:
        rid = str(run.get("id", ""))
        if run_id and rid != run_id:
            continue
        # Store snapshots may attach the event window to each run, while the
        # controller compatibility shape exposes the latest run's events at
        # the top level. Accept both and deduplicate by durable event id.
        events = _rows(run.get("events"))
        if not events:
            events = [event for event in _rows(data.get("events"))
                      if not event.get("run_id") or event.get("run_id") == rid]
        else:
            known = {event.get("event_id") for event in events if event.get("event_id") is not None}
            events.extend(event for event in _rows(data.get("events"))
                          if (not event.get("run_id") or event.get("run_id") == rid)
                          and event.get("event_id") not in known)
        for event in events:
            if event.get("run_id") and event["run_id"] != rid:
                continue
            kind = _text(event.get("kind"), 40)
            labels = {"task_start": "Task started", "task_pass": "Task finished", "task_fail": "Task failed",
                      "planned": "Plan ready", "adaptive_replan": "Plan revised", "replan": "Plan revised", "retry": "Retry"}
            message = _text(event.get("message"))
            prefix = labels.get(kind)
            text = (prefix + ": " + message) if prefix and message else (prefix or message or kind.replace("_", " ").capitalize())
            result.append(_event(rid, text, kind=kind, at=event.get("ts"), final=kind in _URGENT,
                source=event.get("event_id") or str(event.get("ts")) + text))
        if run.get("status") in _TERMINAL:
            plan = _rows(run.get("plan"))
            failed = sum(row.get("status") in _FAILED for row in plan)
            receipt = _receipt_text(run.get("host_receipt"))
            evidence = (" Receipt: " + receipt + ".") if receipt else ""
            text = "Autopilot %s. Result: %s. Where: %s. %s%s Tasks failed: %d." % (
                run["status"], _text(run.get("summary") or run.get("final_report"), 190) or "no output recorded",
                _location(run) or "no project folder recorded", _validation(plan), evidence, failed)
            if run.get("last_error"):
                text += " Reason: " + _text(run["last_error"])
            final_at = run.get("finished_ts") or run.get("updated_ts")
            result.append(_event(rid, text, kind="final", final=True, at=final_at,
                                 source=rid + ":" + run["status"] + ":" + str(final_at or "")))
    return _rate_limit(result, 1000)


def activity_progress(snapshot: Any, run_id=""):
    data = snapshot if isinstance(snapshot, dict) else {}
    responses = _rows(data.get("active")) + _rows([data.get("latest")])
    if not responses:
        responses = [{"id": run_id, "events": data.get("events", data.get("event_ring"))}]
    result = []
    for response in responses:
        rid = str(response.get("id") or run_id)
        if run_id and rid != run_id:
            continue
        for event in _rows(response.get("events")):
            kind = _text(event.get("kind"), 40)
            failed_model_call = (kind in {"model_call", "model_start", "generation", "inference"}
                                 and (event.get("ok") is False or str(event.get("phase", "")).lower() in {"failed", "error"}))
            if kind in _NOISE_EVENTS and not failed_model_call:
                continue
            if failed_model_call:
                kind = "failed"
            urgent = kind in {"failed", "tool_result", "response_error", "response_cancelled", "response_complete", "response_incomplete", "response_unverified"}
            summary = event.get("summary")
            # Some public event fields are preview descriptors, never repr()
            # them or reach around their explicit visibility flag.
            if isinstance(summary, dict):
                summary = summary.get("text", "") if summary.get("available") else ""
            text = _text(summary) or _text(event.get("title")) or kind.replace("_", " ")
            result.append(_event(rid or str(event.get("response_id", "")), text,
                kind="failed" if event.get("ok") is False else kind, final=urgent,
                at=event.get("at", event.get("ts")), source=event.get("seq") or str(event.get("ts")) + text))
        if response.get("status") in {"complete", "error", "cancelled", "incomplete", "unverified"}:
            locations = [_text(row.get("path"), 100) for row in _rows(response.get("files"))[-3:]]
            locations = [path for path in locations if path]
            summary = _text(response.get("result_summary"), 200) or "see the recorded result"
            message = "Agent %s. Result: %s. %s Validation: no validation receipt recorded." % (
                response["status"], summary,
                "Files: " + ", ".join(locations) + "." if locations else "No changed file path recorded.")
            result.append(_event(rid, message, kind="final", final=True,
                at=(response.get("events") or [{}])[-1].get("ts", 0), source=rid + ":" + response["status"]))
    return _rate_limit(result, 1000)


def fanout_progress(snapshot: Any, run_id=""):
    data = snapshot if isinstance(snapshot, dict) else {}
    result = []
    for run in _rows(data.get("runs")):
        rid = str(run.get("id", ""))
        if run_id and rid != run_id:
            continue
        events = _rows(run.get("events"))
        if not events:
            events = _rows(data.get("events"))
        for event in events:
            if event.get("run_id") != rid:
                continue
            kind = _text(event.get("kind"), 40)
            urgent = kind in {"completed", "cancelled", "interrupted", "failed", "result", "answered"}
            result.append(_event(rid, event.get("message") or kind, kind=kind, final=urgent,
                at=event.get("ts"), source=event.get("event_id")))
        if run.get("status") in {"completed", "cancelled", "canceled", "interrupted", "failed", "error"}:
            results = _rows(run.get("results"))
            answered = sum(row.get("status") == "answered" for row in results)
            failed = sum(row.get("status") == "failed" for row in results)
            unknown = sum(row.get("status") == "unknown" for row in results)
            final_at = run.get("finished_ts") or run.get("updated_ts")
            result.append(_event(rid, "Model fanout %s: %d answered, %d failed, %d unknown. Results retained in %s. No validation was run by fanout." % (
                run["status"], answered, failed, unknown, rid), kind="final", final=True,
                at=final_at, source=rid + ":" + run["status"] + ":" + str(final_at or "")))
    return _rate_limit(result, 1000)


def progress(fleet: Any = None, autopilot: Any = None, activity: Any = None,
             run_id: str = "", limit: int = 24, fanout: Any = None) -> list[dict]:
    rows = []
    for source, project in ((fleet, fleet_progress), (autopilot, autopilot_progress),
                            (activity, activity_progress), (fanout, fanout_progress)):
        if source is not None:
            rows.extend(project(source, run_id))
    return _rate_limit(rows, min(int(_number(limit, 24)), 100))


def bounded_progress(rows, run_id="", limit=24):
    """Coalesce authorized child sources under their originating work run."""
    return _rate_limit([{**row, "run_id": run_id or row["run_id"]} for row in rows], limit)


__all__ = ["acknowledgement", "activity_progress", "autopilot_progress", "fanout_progress", "fleet_progress", "progress", "bounded_progress"]
