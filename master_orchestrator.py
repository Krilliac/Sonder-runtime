"""Master/subagent orchestration with live status snapshots."""
from __future__ import annotations

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
    """Return the local fan-out ceiling from available logical CPU capacity.

    Ollama remains the model scheduler, so this is a submission ceiling rather
    than a promise that every request will execute simultaneously. The default
    is two bounded workers per logical CPU, capped by the global safety limit.
    ``TRILOBITE_MAX_AGENTS`` can lower or raise it up to that safety limit.
    """
    logical = max(1, int(os.cpu_count() or 1))
    return max(DEFAULT_MAX_AGENTS, min(ABSOLUTE_MAX_AGENTS, logical * 2))


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
        _AGENTS[agent_id] = {
            "id": agent_id,
            "role": role,
            "parent_id": parent_id,
            "task": task,
            "status": "queued",
            "activity": "queued",
            "started_ts": now,
            "updated_ts": now,
            "updated_seq": next(_UPDATE_SEQUENCE),
            "finished_ts": None,
            "tool_calls": 0,
            "tokens_in": estimate_tokens(task),
            "tokens_out": 0,
            "files": [],
            "summary": "",
            "output": "",
            "error": "",
        }
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


def _finish(agent_id: str, output: str = "", error: str = "") -> str:
    status = "failed" if error else "done"
    update_agent(
        agent_id,
        status=status,
        activity=("failed: %s" % error[:160]) if error else "finished",
        tokens_out=estimate_tokens(output),
        summary=(output or error)[:500],
        output=output,
        error=error,
    )
    return output


def _run_worker(agent_id: str, prompt: str, worker_fn) -> str:
    update_agent(
        agent_id,
        status="running",
        activity="calling model for delegated task",
        tool_calls=1,
    )
    try:
        output = worker_fn(prompt)
    except Exception as exc:  # defensive boundary for worker threads
        _finish(agent_id, error=str(exc))
        return "ERROR: %s" % exc
    return _finish(agent_id, output=output)


def run_inline(task: str, worker_fn) -> dict:
    if requires_repository_tools(task):
        worker_fn = _repository_worker
    master_id = _new_agent("master", task)
    update_agent(master_id, status="running", activity="running inline as master", tool_calls=1)
    try:
        output = worker_fn(task)
    except Exception as exc:
        _finish(master_id, error=str(exc))
        return {"mode": "inline", "master_id": master_id, "output": "ERROR: %s" % exc}
    _finish(master_id, output=output)
    return {"mode": "inline", "master_id": master_id, "output": output}


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
    master_id = _new_agent("master", task)
    update_agent(
        master_id,
        status="running",
        activity="delegating task to %d parallel agent(s)" % agents,
    )
    repository_task = requires_repository_tools(task)
    prompts = _subtask_prompts(task, agents, tool_access=repository_task)
    child_ids = [_new_agent("agent", prompt, parent_id=master_id) for prompt in prompts]
    outputs = []
    with ThreadPoolExecutor(max_workers=agents) as pool:
        futures = {
            pool.submit(_run_worker, agent_id, prompt, worker_fn): agent_id
            for agent_id, prompt in zip(child_ids, prompts)
        }
        for future in as_completed(futures):
            agent_id = futures[future]
            try:
                outputs.append((agent_id, future.result()))
            except Exception as exc:
                outputs.append((agent_id, "ERROR: %s" % exc))
                _finish(agent_id, error=str(exc))
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
                "outputs": [],
                "output": merged,
            }
    update_agent(master_id, activity="auditing delegated outputs", tool_calls=2)
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
        _finish(master_id, error=str(exc))
        return {
            "mode": "delegated",
            "master_id": master_id,
            "agents": child_ids,
            "outputs": outputs,
            "output": merged,
        }
    _finish(master_id, output=merged)
    return {
        "mode": "delegated",
        "master_id": master_id,
        "agents": child_ids,
        "outputs": outputs,
        "output": merged,
    }


def snapshot(include_finished: bool = True, limit: int = 20) -> dict:
    with _LOCK:
        rows = list(_AGENTS.values())
        if not include_finished:
            rows = [r for r in rows if r.get("status") not in ("done", "failed", "cancelled")]
        rows.sort(key=lambda r: r.get("updated_seq") or 0, reverse=True)
        rows = [dict(r) for r in rows[: max(1, int(limit or 20))]]
        events = list(_EVENTS[-_MAX_EVENTS:])
    active = [r for r in rows if r.get("status") in ("queued", "running")]
    return {
        "active_agents": len(active),
        "total_listed": len(rows),
        "agents": rows,
        "events": events,
        "tokens_in": sum(int(r.get("tokens_in") or 0) for r in rows),
        "tokens_out": sum(int(r.get("tokens_out") or 0) for r in rows),
        "latest_master_result": next(
            (
                r.get("output") or ""
                for r in rows
                if r.get("role") == "master" and r.get("status") == "done" and r.get("output")
            ),
            "",
        ),
    }


def format_snapshot(data: dict) -> str:
    lines = [
        "master orchestrator status",
        "  active agents: %s" % data.get("active_agents", 0),
        "  tokens in/out: %s/%s" % (data.get("tokens_in", 0), data.get("tokens_out", 0)),
    ]
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

