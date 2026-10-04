"""Compose narration from host decisions and the existing retained event sources."""
from __future__ import annotations

import functools
import contextvars
import inspect
import logging
import os
import sys
import threading
import time

from sonder_runtime.application.ports import work_narration as port
from sonder_runtime.adapters.observability import activity_tracker
from sonder_runtime.adapters.persistence import autopilot_store, fleet_store, fanout_store
from sonder_runtime.domain.work_narration import acknowledgement, bounded_progress, progress
from sonder_runtime.adapters.observability.work_narration_facts import configured_host
from sonder_runtime.platform.runtime_threads import Thread

_LOG = logging.getLogger(__name__)
work_scope = port.scope
qualify_activity_id = port.qualify_activity_id
_REPL_STOP = contextvars.ContextVar("narration_repl_stop", default=None)


def _get(runtime, name, default=None):
    return runtime.get(name, default) if isinstance(runtime, dict) else getattr(runtime, name, default)


def prepare_ack(runtime, goal, mode, project="", reason="", run_id="", agents=None,
                worker_cap=None, tier="auto", tasks=()):
    """Read configured host data only: no discovery, inference or model probes."""
    policy = _get(runtime, "runtime_policy")
    if tier in {"", "auto"} and policy is not None:
        tier = policy.route_tier(mode if mode in policy.ROUTING_LANES else "workbench",
                                 _get(runtime, "_RUNTIME_POLICY"), fallback="code")
    elif tier in {"", "auto"}:
        tier = "code"
    slots = None
    if mode == "fleet":
        orchestrator = _get(runtime, "master_orchestrator")
        if orchestrator is not None:
            infer_cap = getattr(orchestrator, "requested_worker_cap", None)
            worker_cap = worker_cap or (infer_cap(goal) if callable(infer_cap) else None)
            agents = agents or worker_cap or orchestrator.max_agents()
            capacity = (orchestrator.capacity(agents, worker_cap=worker_cap)
                        if worker_cap else orchestrator.capacity(agents))
            slots = capacity.get("worker_slots")
            agents = capacity.get("requested_agents", agents)
    resolve = _get(runtime, "_resolve_project")
    project = resolve(project) if callable(resolve) else project
    host = configured_host(runtime, tier)
    if mode == "fanout":
        tier, host = "selected fanout models", "workers selected by the existing fanout policy"
    if not project:
        project = "no project folder selected; the lane will report its output folder"
    elif not os.path.isabs(str(project)):
        project = "project " + str(project) + " (folder resolved by the lane)"
    controls = {
        "fleet": ("/agents", "/agentcancel <master-id>"),
        "autopilot": ("/autopilot status", "/autopilot cancel <run-id>"),
        "fanout": ("model_fanout_status('<run-id>')", "model_fanout_cancel('<run-id>')"),
    }
    watch, cancel = controls.get(mode, ("the live conversation", "Ctrl+C"))
    if run_id.startswith("wr-"):
        watch, cancel = "GET /v1/work-runs/" + run_id, "POST /v1/work-runs/" + run_id + "/cancel"
    return acknowledgement(goal=goal, mode=mode, project=project,
                           tier=tier,
                           host=host, reason=reason,
                           run_id=run_id, agents=agents, worker_slots=slots,
                           watch=watch, cancel=cancel, tasks=tasks)


def _sources(links):
    fleet = {"agents": [], "events": []}
    autopilot = {"runs": [], "events": []}
    fanout = {"runs": [], "events": []}
    missing = False
    for item in links[:16]:
        kind, identity = item.get("kind"), item.get("id", "")
        if not identity:
            continue
        if kind == "fleet":
            root = fleet_store.get_agent(identity)
            if root is None or root.get("id") != identity:
                missing = True
                continue
            rows = fleet_store.list_agents_scoped(root.get("owner_id", ""),
                project=root.get("project", ""), parent_id=identity, limit=200)
            if not any(row["id"] == identity for row in rows):
                rows.append(root)
            fleet["agents"].extend(rows)
            ids = {row["id"] for row in rows}
            fleet["events"].extend(fleet_store.events_for_agents(ids))
        elif kind == "autopilot":
            row = autopilot_store.get_run(identity)
            if row is None or row.get("id") != identity:
                missing = True
                continue
            autopilot["runs"].append(row)
            autopilot["events"].extend(autopilot_store.events(identity, limit=80))
        elif kind == "fanout":
            row = fanout_store.get_run(identity)
            if row is None:
                missing = True
            else:
                row["results"] = fanout_store.list_results(identity, include_answers=False)
                fanout["runs"].append(row)
                fanout["events"].extend(fanout_store.events(identity, limit=80))
    return fleet, autopilot, fanout, missing


def enrich_work_record(record, *, include_detail=False):
    """Call only AFTER the work store's exact owner lookup succeeded."""
    if record is None:
        return None
    out = dict(record)
    narration = record.get("narration") or {}
    links = narration.get("links") or []
    fleet, autopilot, fanout, missing = _sources(links)
    activity_ids = {port.local_activity_id(narration.get("activity_id", ""))}
    activity_ids.update(port.local_activity_id(item.get("id", "")) for item in links if item.get("kind") == "activity")
    activity_ids.discard("")
    raw = activity_tracker.snapshot()
    rows = [*raw.get("active", []), raw.get("latest")]
    own = [row for row in rows if isinstance(row, dict) and row.get("id") in activity_ids]
    activity = activity_tracker.public_snapshot({"active": own}, include_detail=include_detail)
    lines = progress(fleet=fleet, autopilot=autopilot, activity=activity, fanout=fanout)
    # The event ring retains events after another request becomes "latest".
    # Only this exact, owner-bound response may be projected.
    if not own and activity_ids:
        events = [e for e in raw.get("event_ring", []) if e.get("response_id") in activity_ids]
        projected = activity_tracker.public_snapshot({"active": [
            {"id": rid, "events": [e for e in events if e.get("response_id") == rid]}
            for rid in sorted(activity_ids)
        ]}, include_detail=include_detail)
        lines += progress(activity=projected)
    active = any(row.get("status") in {"queued", "running"} for row in fleet["agents"])
    active |= any(row.get("status") in {"ready", "planning", "running"} for row in autopilot["runs"])
    active |= any(row.get("status") not in {"completed", "cancelled", "interrupted"}
                  for row in fanout["runs"])
    complete = record.get("status") != "running" and not active and not missing
    summary = ""
    if complete:
        final_rows = [line for line in lines if line.get("kind") in {"final", "summary"}]
        durable_finals = [line for line in final_rows if line.get("run_id") not in activity_ids]
        finals = [line["text"] for line in (durable_finals or final_rows)]
        summary = "\n".join(dict.fromkeys(finals))
        if not summary:
            summary = "Work %s. %s" % (record.get("status", "unknown"),
                "Result is available in this conversation; no validation receipt was recorded."
                if record.get("output") else "No output or validation receipt was recorded.")
        if not any(line["text"] == summary for line in lines):
            lines.append({"id": record["id"] + ":final", "run_id": record["id"],
                          "text": summary, "kind": "summary", "at": record.get("updated_at", 0), "final": True})
    out.update(progress=bounded_progress(lines, record["id"]), progress_complete=complete, final_summary=summary)
    return out


def cancel_linked_work(record):
    """Cancel only host-linked runs from an already owner-authorized receipt."""
    if record is None:
        return None
    out = dict(record)
    for item in (record.get("narration") or {}).get("links", [])[:16]:
        identity = item.get("id", "")
        if not identity:
            continue
        kind = item.get("kind")
        if kind == "fleet":
            row = fleet_store.get_agent(identity)
            if row is not None and row.get("id") == identity:
                fleet_store.cancel_agents(identity)
                out["cancel_requested"] = True
        elif kind == "autopilot":
            row = autopilot_store.get_run(identity)
            if row is not None and row.get("id") == identity:
                autopilot_store.request_cancel(identity)
                out["cancel_requested"] = True
        elif kind == "fanout":
            if fanout_store.get_run(identity) is not None:
                fanout_store.request_cancel(identity)
                out["cancel_requested"] = True
    return out


def enrich_status(payload, include_detail=False):
    out = dict(payload)
    # Existing endpoint authority and activity-detail policy remain decisive.
    out["progress"] = progress(fleet=payload.get("agents"),
                               autopilot=payload.get("autopilot"), activity=payload.get("activity"))
    return out


def narrated(mode, runtime):
    """Announce entry into an execution lane when a transport installed a sink."""
    def decorate(function):
        signature = inspect.signature(function)
        @functools.wraps(function)
        def invoke(*args, **kwargs):
            if port.current() is None and port.writer() is None:
                return function(*args, **kwargs)
            values = signature.bind(*args, **kwargs).arguments
            for name, parameter in signature.parameters.items():
                if name not in values and parameter.default is not inspect.Parameter.empty:
                    values[name] = parameter.default
            selected = values.get("mode", mode)
            if selected in {"ask", "status"}:
                orchestrator = _get(runtime(), "master_orchestrator")
                if (mode != "fleet" or selected != "ask" or orchestrator is None
                        or not orchestrator.requests_fleet(values.get("task", ""))):
                    return function(*args, **kwargs)
            actual_mode = "workbench" if mode == "fleet" and selected in {"inline", "master"} else mode
            agents = values.get("agents")
            if mode == "fleet" and selected in {"delegate", "delegated", "agents", "parallel"}:
                agents = agents or values.get("worker_cap") or 3
            text = prepare_ack(runtime(), values.get("task") or values.get("objective")
                or values.get("prompt", ""), actual_mode, project=values.get("project", ""),
                reason=port.route_reason() or "explicit %s request" % mode, agents=agents,
                worker_cap=values.get("worker_cap"), tier=values.get("tier", "auto"))
            if port.current() is not None:
                activity_tracker.record_event("work_plan", title="Selected " + actual_mode, summary=text)
                return function(*args, **kwargs)
            with port.scope() as state:
                state.acknowledgement = text
                write = port.writer()
                write(state.acknowledgement)
                state.activity_id = port.qualify_activity_id(activity_tracker.current_response_id())
                finished = threading.Event()
                _watch(state, write, finished)
                try:
                    result = function(*args, **kwargs)
                except BaseException:
                    state.outcome = "failed"
                    write("Work failed; inspect the error below.")
                    raise
                finally:
                    finished.set()
                return result
        return invoke
    return decorate


def _watch(state, write, finished):
    """A bounded presentation cursor over retained events, not a run registry."""
    stopped = _REPL_STOP.get() or threading.Event()
    def watch():
        seen = set()
        last_update = 0.0
        deadline = time.monotonic() + 24 * 3600
        while not stopped.is_set() and time.monotonic() < deadline:
            try:
                record = enrich_work_record({"id": state.run_id or "repl",
                    "status": state.outcome if finished.is_set() else "running",
                    "narration": {"links": list(state.links), "activity_id": state.activity_id}}, include_detail=True)
                for item in record["progress"]:
                    now = time.monotonic()
                    if item["id"] not in seen and (item.get("final") or now - last_update >= 10):
                        write(item["text"])
                        if not item.get("final"):
                            last_update = now
                seen = {item["id"] for item in record["progress"]}
                if record["progress_complete"]:
                    return
            except Exception:
                _LOG.warning("narration progress unavailable", exc_info=True)
                return
            stopped.wait(2)
    Thread(target=watch, name="sonder-repl-progress", daemon=True).start()


def repl_narration(function):
    @functools.wraps(function)
    def invoke(*args, **kwargs):
        def write(text):
            from sonder_runtime.interfaces.repl.style import safe_text
            emit = getattr(sys.stdout, "emit_event", None)
            if callable(emit):
                emit("progress", safe_text(text))
            else:
                print(safe_text(text), flush=True)
        stopped = threading.Event()
        token = _REPL_STOP.set(stopped)
        try:
            with port.output(write):
                return function(*args, **kwargs)
        finally:
            stopped.set()
            _REPL_STOP.reset(token)
    return invoke
