"""Cross-subsystem composition bridges.

Connects Goal, Autopilot, Workflow, Task, Memory, Training, Fleet, SelfMod,
and Persona subsystems so lifecycle events on one propagate to the others.

Trust boundary: this module NEVER promotes user authority.  Operations that
require actor="user" (goal completion, selfmod deploy, training deploy) still
require it — composition connects subsystems, it does not bypass any gate.

Every bridge function is a pure composition of existing subsystem calls.  No
bridge introduces new state beyond what the binding store tracks.
"""
from __future__ import annotations

import functools
import logging

logger = logging.getLogger(__name__)

# --- call-tracking decorator for auditing bridge usage ---
_call_counts: dict[str, int] = {}


def _track_call(fn):
    """Increment a per-function counter on each call.

    Usage data lives in ``composition._call_counts`` keyed by function name.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        _call_counts[fn.__name__] = _call_counts.get(fn.__name__, 0) + 1
        return fn(*args, **kwargs)
    return wrapper


@_track_call
def mission_start(
    objective: str,
    criteria: str = "",
    *,
    auto: bool = False,
    plan: bool = False,
    policy: str = "workspace",
    tier: str = "auto",
    allow_web: bool = True,
    project: str = "",
    request_owner: str = "",
) -> dict:
    """Set a goal, optionally decompose into tasks, optionally launch autopilot.

    This is the primary unified entry point.  It always creates a goal; the
    ``plan`` and ``auto`` flags control how much execution machinery spins up.
    """
    import goal_store
    from sonder_runtime.adapters.persistence import composition_store

    goal = goal_store.set_goal(objective, criteria, origin="mission")
    result = {
        "goal": goal,
        "plan": None,
        "autopilot": None,
        "bindings": [],
    }

    if plan and goal.get("criteria"):
        plan_result = goal_to_plan(goal)
        result["plan"] = plan_result

    if auto:
        ap_result = goal_to_autopilot(
            goal, policy=policy, tier=tier, allow_web=allow_web, project=project,
            request_owner=request_owner,
        )
        result["autopilot"] = ap_result

    return result


@_track_call
def mission_status(scope: str = "") -> dict:
    """Unified view: active goal + bound autopilot runs + linked tasks."""
    import goal_store
    from sonder_runtime.adapters.persistence import composition_store
    import autopilot_controller

    goal = goal_store.get_active(scope)
    result = {
        "goal": goal,
        "autopilot_runs": [],
        "task_plans": [],
        "workflows": [],
        "bindings": [],
    }
    if not goal:
        return result

    bindings = composition_store.lookup_targets(
        "goal", goal["id"],
    )
    result["bindings"] = bindings

    for binding in bindings:
        if binding["target_type"] == "autopilot":
            try:
                run = autopilot_controller.snapshot(limit=1)
                latest = run.get("latest")
                if latest and latest.get("id") == binding["target_id"]:
                    result["autopilot_runs"].append(latest)
            except Exception:
                pass
        elif binding["target_type"] == "task":
            result["task_plans"].append({
                "binding_id": binding["id"],
                "task_id": binding["target_id"],
            })
        elif binding["target_type"] == "workflow":
            result["workflows"].append({
                "binding_id": binding["id"],
                "workflow_name": binding.get("metadata", {}).get("workflow_name", ""),
            })

    return result


@_track_call
def goal_to_autopilot(
    goal: dict,
    *,
    policy: str = "workspace",
    tier: str = "auto",
    allow_web: bool = True,
    project: str = "",
    request_owner: str = "",
) -> dict:
    """Launch autopilot bound to a goal's objective and criteria."""
    from sonder_runtime.adapters.persistence import (
        autopilot_store,
        composition_store,
    )

    goal_id = goal.get("id", "")
    objective = goal.get("objective", "")
    if not objective:
        return {"error": "goal has no objective"}

    criteria = goal.get("criteria", [])
    if criteria:
        objective_with_criteria = "%s\n\nSuccess criteria:\n%s" % (
            objective,
            "\n".join("- %s" % c for c in criteria),
        )
    else:
        objective_with_criteria = objective

    try:
        run = autopilot_store.create_run(
            objective_with_criteria,
            project=project,
            request_owner=request_owner,
            tier=tier,
            policy=policy,
            allow_web=allow_web,
        )
    except (ValueError, OSError) as exc:
        return {"error": "autopilot creation failed: %s" % exc}

    composition_store.bind(
        source_type="goal",
        source_id=goal_id,
        target_type="autopilot",
        target_id=run["id"],
        kind="drives",
        metadata={"objective": objective[:500]},
    )

    return {"run_id": run["id"], "goal_id": goal_id, "status": run.get("status")}


@_track_call
def goal_to_plan(goal: dict) -> dict:
    """Decompose goal criteria into a task plan."""
    from sonder_runtime.adapters.persistence import composition_store

    goal_id = goal.get("id", "")
    objective = goal.get("objective", "")
    criteria = goal.get("criteria", [])

    if not criteria:
        return {"error": "goal has no criteria to decompose"}

    steps = [{"title": c, "detail": ""} for c in criteria]

    composition_store.bind(
        source_type="goal",
        source_id=goal_id,
        target_type="task",
        target_id="plan:%s" % goal_id,
        kind="decomposes",
        metadata={"step_count": len(steps)},
    )

    return {
        "goal_id": goal_id,
        "plan_title": objective,
        "steps": steps,
        "step_count": len(steps),
    }


@_track_call
def on_autopilot_terminal(run: dict) -> dict:
    """Called when an autopilot run reaches a terminal state.

    Updates any bound goal with an outcome note and records outcomes to memory.
    Returns a summary of propagated effects.
    """
    import goal_store
    from sonder_runtime.adapters.persistence import composition_store

    run_id = run.get("id", "")
    status = run.get("status", "")
    effects = {"goal_updated": False, "bindings_closed": 0, "notes_added": []}

    bindings = composition_store.lookup_sources(
        "autopilot", run_id, source_type="goal",
    )

    for binding in bindings:
        goal_id = binding["source_id"]
        goal = goal_store.get_active()
        if goal and goal["id"] == goal_id:
            summary = run.get("summary", run.get("objective", ""))[:500]
            if status == "completed":
                note = "autopilot completed: %s" % summary
            elif status == "failed":
                note = "autopilot failed: %s" % summary
            elif status == "cancelled":
                note = "autopilot cancelled: %s" % summary
            else:
                note = "autopilot ended (%s): %s" % (status, summary)

            goal_store.add_note(note)
            effects["goal_updated"] = True
            effects["notes_added"].append(note)

        composition_store.complete_binding(
            binding["id"],
            reason="autopilot %s" % status,
        )
        effects["bindings_closed"] += 1

    return effects


@_track_call
def composition_status() -> dict:
    """Overview of all active cross-subsystem bindings."""
    from sonder_runtime.adapters.persistence import composition_store

    bindings = composition_store.active_bindings(limit=100)
    by_kind = {}
    for b in bindings:
        kind = b.get("kind", "unknown")
        by_kind.setdefault(kind, []).append(b)

    return {
        "active_bindings": len(bindings),
        "by_kind": {k: len(v) for k, v in by_kind.items()},
        "bindings": bindings[:20],
    }


@_track_call
def format_mission_status(data: dict) -> str:
    """Human-readable mission status combining goal + autopilot + tasks."""
    lines = []
    goal = data.get("goal")
    if not goal:
        return "no active mission (no active goal)"

    lines.append("mission: %s" % goal.get("objective", ""))
    lines.append("  goal: %s [%s]" % (goal.get("id", ""), goal.get("status", "")))
    for criterion in goal.get("criteria", []):
        lines.append("    criterion: %s" % criterion)
    for note in (goal.get("notes") or [])[-3:]:
        lines.append("    note: %s" % note.get("text", ""))

    for run in data.get("autopilot_runs", []):
        lines.append("  autopilot: %s [%s/%s]" % (
            run.get("id", ""), run.get("status", ""), run.get("phase", ""),
        ))
        plan = run.get("plan", [])
        if plan:
            passed = sum(1 for t in plan if t.get("status") == "passed")
            pending = sum(1 for t in plan if t.get("status") == "pending")
            lines.append("    tasks: %d passed, %d pending, %d total" % (
                passed, pending, len(plan),
            ))

    plans = data.get("task_plans", [])
    if plans:
        lines.append("  task plans: %d linked" % len(plans))

    workflows = data.get("workflows", [])
    if workflows:
        lines.append("  workflows: %s" % ", ".join(
            w.get("workflow_name", "?") for w in workflows
        ))

    bindings = data.get("bindings", [])
    if bindings:
        lines.append("  bindings: %d active" % len(bindings))

    return "\n".join(lines)


@_track_call
def format_composition_status(data: dict) -> str:
    """Human-readable composition status."""
    lines = [
        "composition status",
        "  active bindings: %d" % data.get("active_bindings", 0),
    ]
    for kind, count in (data.get("by_kind") or {}).items():
        lines.append("    %s: %d" % (kind, count))
    for binding in (data.get("bindings") or [])[:10]:
        lines.append("  %s %s:%s -[%s]-> %s:%s" % (
            binding.get("id", ""),
            binding.get("source_type", ""),
            binding.get("source_id", "")[:20],
            binding.get("kind", ""),
            binding.get("target_type", ""),
            binding.get("target_id", "")[:20],
        ))
    return "\n".join(lines)


__all__ = [
    "composition_status",
    "format_composition_status",
    "format_mission_status",
    "goal_to_autopilot",
    "goal_to_plan",
    "mission_start",
    "mission_status",
    "on_autopilot_terminal",
]
