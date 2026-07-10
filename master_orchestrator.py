"""Master/subagent orchestration with live status snapshots."""
from __future__ import annotations

import ctypes
import itertools
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed


_LOCK = threading.RLock()
_AGENTS = {}
_EVENTS = []
_UPDATE_SEQUENCE = itertools.count()
_MAX_EVENTS = 80
DEFAULT_MAX_AGENTS = 16
ABSOLUTE_MAX_AGENTS = 64
DEFAULT_MAX_WORKERS = 8
ABSOLUTE_MAX_WORKERS = 16
RAM_RESERVE_BYTES = int(1.5 * 1024 ** 3)
RAM_PER_WORKER_BYTES = int(1.25 * 1024 ** 3)

EVIDENCE_REQUIRED = (
    "EVIDENCE_REQUIRED: guarded source evidence was unavailable. Authorize the "
    "repository in file_roots.local, embed the relevant source excerpts, or use "
    "the tool-using agent surface to inspect it first. No unsupported answer was produced."
)

_REPOSITORY_REQUEST = re.compile(
    r"(?:\brepository\s*:|\brepo\s*:|\bcurrent\s+(?:file|code|diff|uncommitted)|"
    r"\b(?:inspect|read|review|audit|edit|fix)\b.{0,40}\b(?:repo|repository|codebase|"
    r"workspace|files?)\b|\buse\s+(?:local\s+)?file[- ]reading\s+tools?\b)",
    re.IGNORECASE | re.DOTALL,
)
_EMBEDDED_EVIDENCE = re.compile(
    r"(?:```|\bsource\s+excerpts?\s*:|\bbegin\s+file\b|\bpatch\s*:|\bdiff\s+--git\b)",
    re.IGNORECASE,
)
_FLEET_REQUEST = re.compile(
    r"(?:\bfleet\b|\bswarm\b|\bfan[- ]?out\b|\bparallel\s+agents?\b|"
    r"\bspawn\s+(?:as\s+much|as\s+many|the\s+maximum|maximum|parallel|subagents?)\b|"
    r"\bas\s+many\s+(?:subagents?|agents?)\b|\bparallel\s+workflow\b|"
    r"\bspawn\s+workflow\b|\bworkflow\b|\bmax(?:imum)?\s+agents?\b)",
    re.IGNORECASE,
)


def hardware_max_agents() -> int:
    """Return the local queued-candidate ceiling from logical CPU capacity.

    This controls breadth/diversity, not simultaneous model calls. Concurrent
    execution is separately constrained by :func:`capacity`. The default queues
    two candidates per logical CPU, capped by the global safety limit.
    ``TRILOBITE_MAX_AGENTS`` can lower or raise it up to that safety limit.
    """
    logical = max(1, int(os.cpu_count() or 1))
    return max(DEFAULT_MAX_AGENTS, min(ABSOLUTE_MAX_AGENTS, logical * 2))


def physical_memory_bytes() -> tuple[int, int]:
    """Return ``(total, available)`` physical RAM, or zeros if unavailable."""
    if os.name == "nt":
        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.length = ctypes.sizeof(MemoryStatusEx)
        try:
            ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        except (AttributeError, OSError):
            ok = False
        if ok:
            return int(status.total_physical), int(status.available_physical)
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total = page_size * int(os.sysconf("SC_PHYS_PAGES"))
        available = page_size * int(os.sysconf("SC_AVPHYS_PAGES"))
        return total, available
    except (AttributeError, OSError, TypeError, ValueError):
        return 0, 0


def capacity(requested_agents: int | str | None = None) -> dict:
    """Describe queued-agent ceiling separately from safe concurrent slots."""
    logical = max(1, int(os.cpu_count() or 1))
    ceiling = max_agents()
    requested = clamp_agent_count(
        requested_agents, default=ceiling if requested_agents is None else 3,
    )
    total, available = physical_memory_bytes()
    cpu_slots = max(1, min(DEFAULT_MAX_WORKERS, logical // 4 or 1))
    if available > 0:
        usable = max(0, available - RAM_RESERVE_BYTES)
        ram_slots = max(1, usable // RAM_PER_WORKER_BYTES)
    else:
        ram_slots = DEFAULT_MAX_WORKERS
    automatic = max(1, min(requested, cpu_slots, int(ram_slots), DEFAULT_MAX_WORKERS))
    source = "auto"
    slots = automatic
    raw_override = os.environ.get("TRILOBITE_PARALLEL_WORKERS", "").strip()
    if raw_override:
        try:
            override = int(raw_override)
        except (TypeError, ValueError):
            override = automatic
            source = "invalid override; auto"
        else:
            slots = max(1, min(override, requested, ABSOLUTE_MAX_WORKERS))
            source = "TRILOBITE_PARALLEL_WORKERS"
    return {
        "logical_cpus": logical,
        "total_memory_bytes": total,
        "available_memory_bytes": available,
        "agent_ceiling": ceiling,
        "requested_agents": requested,
        "worker_slots": slots,
        "automatic_worker_slots": automatic,
        "source": source,
        "ram_reserve_bytes": RAM_RESERVE_BYTES,
        "ram_per_worker_bytes": RAM_PER_WORKER_BYTES,
    }


def parallel_worker_slots(requested_agents: int | str | None = None) -> int:
    return int(capacity(requested_agents)["worker_slots"])


def requests_fleet(task: str) -> bool:
    """Recognize explicit natural-language requests for maximum fan-out."""
    return bool(_FLEET_REQUEST.search(task or ""))


def requires_repository_tools(task: str) -> bool:
    """Return true when a task asks the model to inspect external repo state."""
    task = task or ""
    return bool(_REPOSITORY_REQUEST.search(task) and not _EMBEDDED_EVIDENCE.search(task))


def evidence_gate(task: str, tools_available: bool = True) -> str:
    """Refuse ungrounded repo inspection only when guarded tools are unavailable."""
    if requires_repository_tools(task) and not tools_available:
        return EVIDENCE_REQUIRED
    return ""


def _repository_worker(prompt: str) -> str:
    """Lazily enter server's guarded agent loop without an import cycle."""
    import server

    return server._agent_impl(
        prompt,
        tier="code",
        max_steps=8,
        allow_web=False,
        require_file_evidence=True,
        read_only=True,
        include_evidence=True,
    )


def max_agents() -> int:
    """Configured upper bound for delegated subagents."""
    raw = os.environ.get("TRILOBITE_MAX_AGENTS", str(hardware_max_agents()))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = hardware_max_agents()
    return max(1, min(value, ABSOLUTE_MAX_AGENTS))


def clamp_agent_count(count: int | str | None, default: int = 3) -> int:
    try:
        requested = int(count or default)
    except (TypeError, ValueError):
        requested = default
    return max(1, min(requested, max_agents()))


def _now() -> float:
    return time.time()


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def estimate_tokens(text: str) -> int:
    text = text or ""
    return max(1, (len(text) + 3) // 4) if text else 0


def _event(agent_id: str, message: str) -> None:
    with _LOCK:
        _EVENTS.append({
            "ts": _stamp(),
            "agent_id": agent_id,
            "message": message,
        })
        del _EVENTS[:-_MAX_EVENTS]


def _new_agent(role: str, task: str, parent_id: str = "") -> str:
    agent_id = "%s-%s" % (role, uuid.uuid4().hex[:8])
    now = _now()
    with _LOCK:
        inherited_cancel = bool(
            parent_id and (_AGENTS.get(parent_id) or {}).get("cancel_requested")
        )
        _AGENTS[agent_id] = {
            "id": agent_id,
            "role": role,
            "parent_id": parent_id,
            "task": task,
            "status": "cancelled" if inherited_cancel else "queued",
            "activity": "cancelled with parent" if inherited_cancel else "queued",
            "started_ts": now,
            "updated_ts": now,
            "updated_seq": next(_UPDATE_SEQUENCE),
            "finished_ts": now if inherited_cancel else None,
            "tool_calls": 0,
            "tokens_in": estimate_tokens(task),
            "tokens_out": 0,
            "files": [],
            "summary": "cancelled before model call" if inherited_cancel else "",
            "output": "",
            "error": "",
            "cancel_requested": inherited_cancel,
            "in_model_call": False,
        }
    if inherited_cancel:
        _event(agent_id, "cancelled with parent before start")
    else:
        _event(agent_id, "queued: %s" % task[:140])
    return agent_id


def update_agent(agent_id: str, **changes) -> None:
    with _LOCK:
        row = _AGENTS.get(agent_id)
        if not row:
            return
        row.update(changes)
        row["updated_ts"] = _now()
        row["updated_seq"] = next(_UPDATE_SEQUENCE)
        if changes.get("status") in ("done", "failed", "cancelled"):
            row["finished_ts"] = row["updated_ts"]
    if "activity" in changes:
        _event(agent_id, changes["activity"])


def cancel_requested(agent_id: str) -> bool:
    with _LOCK:
        return bool((_AGENTS.get(agent_id) or {}).get("cancel_requested"))


def _start_agent(agent_id: str, activity: str, **changes) -> bool:
    """Atomically move a queued agent to running unless it was cancelled."""
    with _LOCK:
        row = _AGENTS.get(agent_id)
        if not row or row.get("cancel_requested") or row.get("status") != "queued":
            return False
        row.update(changes)
        row["status"] = "running"
        row["activity"] = activity
        row["updated_ts"] = _now()
        row["updated_seq"] = next(_UPDATE_SEQUENCE)
    _event(agent_id, activity)
    return True


def _resolve_cancel_targets(selector: str) -> list[str]:
    value = str(selector or "").strip()
    with _LOCK:
        active = {
            agent_id: row for agent_id, row in _AGENTS.items()
            if row.get("status") in ("queued", "running")
        }
        if value.lower() in ("all", "*"):
            selected = set(active)
        elif not value:
            selected = set()
        elif value in active:
            selected = {value}
        else:
            selected = {
                agent_id for agent_id in active if agent_id.startswith(value)
            }
        # Canceling a master also selects all active descendants.
        changed = True
        while changed:
            changed = False
            for agent_id, row in active.items():
                if row.get("parent_id") in selected and agent_id not in selected:
                    selected.add(agent_id)
                    changed = True
        return sorted(selected)


def request_cancel(selector: str) -> dict:
    """Request cooperative cancellation by exact ID, prefix, or ``all``."""
    running = 0
    model_calls = 0
    queued = 0
    now = _now()
    events = []
    with _LOCK:
        # Keep target resolution and mutation under one re-entrant lock. Any child
        # created after this point sees its parent's cancellation and inherits it.
        targets = _resolve_cancel_targets(selector)
        for agent_id in targets:
            row = _AGENTS.get(agent_id)
            if not row or row.get("status") not in ("queued", "running"):
                continue
            row["cancel_requested"] = True
            row["updated_ts"] = now
            row["updated_seq"] = next(_UPDATE_SEQUENCE)
            if row.get("status") == "queued":
                queued += 1
                row["status"] = "cancelled"
                row["activity"] = "cancelled before start"
                row["finished_ts"] = now
                row["summary"] = "cancelled before model call"
                events.append((agent_id, "cancelled before start"))
            else:
                running += 1
                if row.get("in_model_call"):
                    model_calls += 1
                    row["activity"] = "cancellation requested; waiting for active model call"
                else:
                    row["activity"] = "cancellation requested; stopping after active children"
                events.append((agent_id, row["activity"]))
    for agent_id, message in events:
        _event(agent_id, message)
    return {
        "selector": str(selector or ""),
        "matched": len(targets),
        "running": running,
        "model_calls": model_calls,
        "queued": queued,
        "agent_ids": targets,
        "cooperative": True,
    }


def _finish(agent_id: str, output: str = "", error: str = "") -> str:
    if cancel_requested(agent_id):
        update_agent(
            agent_id,
            status="cancelled",
            activity="cancelled; late result discarded",
            tokens_out=0,
            summary="cancelled; active call returned and its result was discarded",
            output="",
            error="",
            in_model_call=False,
        )
        return "CANCELLED"
    status = "failed" if error else "done"
    update_agent(
        agent_id,
        status=status,
        activity=("failed: %s" % error[:160]) if error else "finished",
        tokens_out=estimate_tokens(output),
        summary=(output or error)[:500],
        output=output,
        error=error,
        in_model_call=False,
    )
    return output


def _run_worker(agent_id: str, prompt: str, worker_fn) -> str:
    if not _start_agent(
        agent_id, "calling model for delegated task", tool_calls=1,
        in_model_call=True,
    ):
        return "CANCELLED"
    try:
        output = worker_fn(prompt)
    except Exception as exc:  # defensive boundary for worker threads
        final = _finish(agent_id, error=str(exc))
        return final if final == "CANCELLED" else "ERROR: %s" % exc
    return _finish(agent_id, output=output)


def run_inline(task: str, worker_fn) -> dict:
    if requires_repository_tools(task):
        worker_fn = _repository_worker
    master_id = _new_agent("master", task)
    if not _start_agent(
        master_id, "running inline as master", tool_calls=1, in_model_call=True,
    ):
        return {"mode": "inline", "master_id": master_id, "output": "CANCELLED"}
    try:
        output = worker_fn(task)
    except Exception as exc:
        _finish(master_id, error=str(exc))
        return {"mode": "inline", "master_id": master_id, "output": "ERROR: %s" % exc}
    final = _finish(master_id, output=output)
    return {"mode": "inline", "master_id": master_id, "output": final}


def _subtask_prompts(task: str, count: int, tool_access: bool = False) -> list[str]:
    count = clamp_agent_count(count, default=1)
    prompts = []
    for i in range(count):
        access_contract = (
            "You have guarded read-only file tools. Inspect the relevant allowed files "
            "before making codebase claims, and never request write/edit/delete tools. "
            if tool_access else
            "This is a greenfield design/implementation task, not a request to inspect "
            "an existing repository. You have no filesystem, shell, web, or hidden tool "
            "access; use the task as the specification and make explicit assumptions. "
        )
        prompts.append(
            "You are delegated subagent %d/%d. %sNever "
            "claim that you inspected, edited, compiled, ran, or verified anything "
            "you were not explicitly shown. Quote the exact supporting excerpt for "
            "each codebase finding; label unsupported possibilities as hypotheses. "
            "If the task explicitly requires current repository evidence and it is "
            "absent, answer EVIDENCE_REQUIRED and list the smallest missing inputs. "
            "For greenfield architecture, design, or implementation requests, make "
            "clearly labeled proposals from the task itself instead of refusing. "
            "Work independently and keep the answer concise."
            "\n\nTask:\n%s" % (i + 1, count, access_contract, task)
        )
    return prompts


def run_delegated(task: str, worker_fn, audit_fn, agents: int = 3) -> dict:
    if requires_repository_tools(task):
        worker_fn = _repository_worker
    agents = clamp_agent_count(agents, default=3)
    worker_slots = parallel_worker_slots(agents)
    master_id = _new_agent("master", task)
    started = _start_agent(
        master_id,
        "queued %d agent(s) across %d worker slot(s)" % (agents, worker_slots),
        requested_agents=agents,
        worker_slots=worker_slots,
    )
    if not started:
        return {
            "mode": "delegated",
            "master_id": master_id,
            "agents": [],
            "worker_slots": worker_slots,
            "outputs": [],
            "output": "CANCELLED",
        }
    repository_task = requires_repository_tools(task)
    prompts = _subtask_prompts(task, agents, tool_access=repository_task)
    child_ids = [_new_agent("agent", prompt, parent_id=master_id) for prompt in prompts]
    outputs = []
    with ThreadPoolExecutor(max_workers=worker_slots) as pool:
        futures = {
            pool.submit(_run_worker, agent_id, prompt, worker_fn): agent_id
            for agent_id, prompt in zip(child_ids, prompts)
        }
        for future in as_completed(futures):
            agent_id = futures[future]
            try:
                output = future.result()
                if output != "CANCELLED":
                    outputs.append((agent_id, output))
            except Exception as exc:
                outputs.append((agent_id, "ERROR: %s" % exc))
                _finish(agent_id, error=str(exc))
    if cancel_requested(master_id):
        final = _finish(master_id)
        return {
            "mode": "delegated",
            "master_id": master_id,
            "agents": child_ids,
            "worker_slots": worker_slots,
            "outputs": outputs,
            "output": final,
        }
    if repository_task:
        outputs = [
            (agent_id, output)
            for agent_id, output in outputs
            if "=== TOOL EVIDENCE ===" in output
        ]
        if not outputs:
            merged = EVIDENCE_REQUIRED
            _finish(master_id, output=merged)
            return {
                "mode": "delegated",
                "master_id": master_id,
                "agents": child_ids,
                "worker_slots": worker_slots,
                "outputs": [],
                "output": merged,
            }
    update_agent(
        master_id,
        activity="auditing delegated outputs",
        tool_calls=2,
        in_model_call=True,
    )
    audit_prompt = [
        "You are the master orchestrator. You also have no filesystem or tool access. "
        "Audit the delegated outputs strictly against evidence quoted in the original "
        "task. Discard invented files, symbols, APIs, edits, test runs, and success "
        "claims. Never convert a proposal into a claim that work was completed. Resolve "
        "conflicts, separate verified findings from hypotheses. For repository tasks, "
        "end with an Evidence gaps section. For greenfield design/build tasks, "
        "implementation plans are valid outputs even when no repository evidence is "
        "provided. Return EVIDENCE_REQUIRED only when the original task explicitly "
        "requires current repository evidence and that evidence is unavailable. "
        "This task is greenfield because it did not ask to inspect an existing "
        "repository; therefore produce a concrete proposal/plan even without file "
        "evidence. For greenfield work, choose sensible defaults for unspecified "
        "libraries, mechanics, assets, and milestones; state those assumptions and "
        "turn them into implementation steps. Do not call ordinary design choices "
        "evidence gaps or ask the user to supply them. Honor explicit constraints "
        "such as no third-party libraries; if a platform API is needed, choose and "
        "name an in-house or OS-native alternative. End greenfield answers with "
        "Decisions made and Open risks, not an Evidence gaps questionnaire.",
        "",
        "Original task:",
        task,
        "",
    ]
    for agent_id, output in outputs:
        audit_prompt.extend(["--- %s ---" % agent_id, output, ""])
    try:
        merged = audit_fn("\n".join(audit_prompt))
    except Exception as exc:
        merged = "ERROR: audit failed: %s" % exc
        final = _finish(master_id, error=str(exc))
        return {
            "mode": "delegated",
            "master_id": master_id,
            "agents": child_ids,
            "worker_slots": worker_slots,
            "outputs": outputs,
            "output": final if final == "CANCELLED" else merged,
        }
    final = _finish(master_id, output=merged)
    return {
        "mode": "delegated",
        "master_id": master_id,
        "agents": child_ids,
        "worker_slots": worker_slots,
        "outputs": outputs,
        "output": final,
    }


def snapshot(include_finished: bool = True, limit: int = 20) -> dict:
    with _LOCK:
        all_rows = list(_AGENTS.values())
        all_rows.sort(key=lambda r: r.get("updated_seq") or 0, reverse=True)
        active_count = sum(
            1 for row in all_rows if row.get("status") in ("queued", "running")
        )
        cancel_pending = sum(
            1 for row in all_rows
            if row.get("cancel_requested") and row.get("status") == "running"
        )
        tokens_in = sum(int(row.get("tokens_in") or 0) for row in all_rows)
        tokens_out = sum(int(row.get("tokens_out") or 0) for row in all_rows)
        latest_result = next(
            (
                row.get("output") or ""
                for row in all_rows
                if row.get("role") == "master"
                and row.get("status") == "done"
                and row.get("output")
            ),
            "",
        )
        rows = all_rows
        if not include_finished:
            rows = [r for r in rows if r.get("status") not in ("done", "failed", "cancelled")]
        rows = [dict(r) for r in rows[: max(1, int(limit or 20))]]
        events = list(_EVENTS[-_MAX_EVENTS:])
    return {
        "active_agents": active_count,
        "cancel_pending": cancel_pending,
        "total_agents": len(all_rows),
        "total_listed": len(rows),
        "agents": rows,
        "events": events,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "latest_master_result": latest_result,
        "capacity": capacity(),
    }


def format_capacity(data: dict | None = None) -> str:
    data = data or capacity()
    gib = float(1024 ** 3)
    total = float(data.get("total_memory_bytes") or 0) / gib
    available = float(data.get("available_memory_bytes") or 0) / gib
    return "\n".join([
        "master orchestration capacity",
        "  logical CPUs: %s | RAM: %.1f/%.1f GiB available" % (
            data.get("logical_cpus", 0), available, total,
        ),
        "  agent ceiling: %s queued | concurrent worker slots: %s" % (
            data.get("agent_ceiling", 0), data.get("worker_slots", 0),
        ),
        "  automatic slots: %s | source: %s" % (
            data.get("automatic_worker_slots", 0), data.get("source", "auto"),
        ),
        "  policy: reserve %.1f GiB, budget %.2f GiB per active worker" % (
            float(data.get("ram_reserve_bytes") or 0) / gib,
            float(data.get("ram_per_worker_bytes") or 0) / gib,
        ),
    ])


def format_snapshot(data: dict) -> str:
    lines = [
        "master orchestrator status",
        "  active agents: %s" % data.get("active_agents", 0),
        "  cancellation pending: %s" % data.get("cancel_pending", 0),
        "  tokens in/out: %s/%s" % (data.get("tokens_in", 0), data.get("tokens_out", 0)),
    ]
    capacity_data = data.get("capacity") or {}
    if capacity_data:
        lines.append("  capacity: %s queued ceiling / %s active worker slot(s) [%s]" % (
            capacity_data.get("agent_ceiling", 0),
            capacity_data.get("worker_slots", 0),
            capacity_data.get("source", "auto"),
        ))
    agents = data.get("agents") or []
    if not agents:
        lines.append("  agents: none yet")
    for row in agents[:12]:
        lines.append("  - %(id)s [%(status)s] %(activity)s" % row)
        lines.append("      task: %s" % (row.get("task") or "")[:180])
    latest_result = data.get("latest_master_result") or ""
    if latest_result:
        lines.extend(["", "latest completed master result:", latest_result[:8000]])
    return "\n".join(lines)


def reset_for_tests() -> None:
    with _LOCK:
        _AGENTS.clear()
        _EVENTS.clear()

