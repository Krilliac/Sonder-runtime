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

import json
import logging
import time

logger = logging.getLogger(__name__)


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
        )
        result["autopilot"] = ap_result

    return result


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


def goal_to_autopilot(
    goal: dict,
    *,
    policy: str = "workspace",
    tier: str = "auto",
    allow_web: bool = True,
    project: str = "",
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


def plan_to_workflow(steps: list, workflow_name: str, goal_id: str = "") -> dict:
    """Serialize task plan steps as a named workflow's actions."""
    from sonder_runtime.adapters.persistence import composition_store

    actions = []
    for step in steps:
        title = step.get("title", "") if isinstance(step, dict) else str(step)
        actions.append({
            "type": "sonder",
            "text": title,
        })

    if goal_id:
        composition_store.bind(
            source_type="task",
            source_id="plan:%s" % goal_id,
            target_type="workflow",
            target_id=workflow_name,
            kind="produces",
            metadata={"action_count": len(actions)},
        )

    return {
        "workflow_name": workflow_name,
        "actions": actions,
        "action_count": len(actions),
    }


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


def on_workflow_complete(
    workflow_name: str,
    result: dict,
    task_id: str = "",
) -> dict:
    """Called when a workflow run completes. Updates bound goal/task."""
    import goal_store
    from sonder_runtime.adapters.persistence import composition_store

    effects = {"goal_noted": False, "bindings_closed": 0}

    bindings = composition_store.lookup_sources(
        "workflow", workflow_name,
    )

    ok = result.get("ok", False)
    for binding in bindings:
        if binding["source_type"] == "goal":
            goal = goal_store.get_active()
            if goal and goal["id"] == binding["source_id"]:
                status_word = "succeeded" if ok else "failed"
                goal_store.add_note(
                    "workflow '%s' %s" % (workflow_name, status_word),
                )
                effects["goal_noted"] = True

        if binding["source_type"] == "task":
            pass

        status = "completed" if ok else "broken"
        composition_store.complete_binding(binding["id"], reason="workflow %s" % status)
        effects["bindings_closed"] += 1

    return effects


def autopilot_outcomes_to_memory(run: dict) -> dict:
    """Record autopilot task outcomes as learning signals.

    Maps autopilot plan tasks with their pass/fail status into the memory
    subsystem so future runs benefit from accumulated experience.
    """
    from sonder_runtime.adapters.persistence import composition_store

    plan = run.get("plan", [])
    recorded = 0
    for task in plan:
        task_status = task.get("status", "")
        if task_status in ("passed", "failed"):
            composition_store.bind(
                source_type="autopilot",
                source_id=run.get("id", ""),
                target_type="memory",
                target_id="outcome:%s:%s" % (run.get("id", ""), task.get("id", "")),
                kind="produces",
                metadata={
                    "task_title": task.get("title", "")[:200],
                    "task_kind": task.get("kind", ""),
                    "task_status": task_status,
                    "objective": run.get("objective", "")[:500],
                },
            )
            recorded += 1

    return {"recorded": recorded, "total_tasks": len(plan)}


def selfmod_observations_to_goals(observations: list[dict]) -> dict:
    """Convert selfmod observations into goal proposals.

    SelfMod in observe mode identifies improvement opportunities.  This bridge
    feeds them into the goal proposal queue for user review.
    """
    import goal_store

    proposed = 0
    skipped = 0
    for obs in observations:
        description = obs.get("description", "")
        if not description:
            skipped += 1
            continue
        objective = "selfmod: %s" % description[:2000]
        criteria = []
        if obs.get("files"):
            criteria.append("affects files: %s" % ", ".join(
                str(f)[:100] for f in obs["files"][:5]
            ))
        if obs.get("severity"):
            criteria.append("severity: %s" % obs["severity"])
        result = goal_store.propose(objective, criteria, source="selfmod")
        if result:
            proposed += 1
        else:
            skipped += 1

    return {"proposed": proposed, "skipped": skipped}


def training_to_campaign(task_spec: dict) -> dict:
    """Wrap a training task specification as a campaign execution descriptor.

    Maps training task fields (prompt, expected_output, language, assert_checks)
    into the campaign generate-compile-execute-record pipeline shape.
    """
    from sonder_runtime.adapters.persistence import composition_store

    task_id = task_spec.get("id", "training-task")
    prompt = task_spec.get("prompt", "")
    language = task_spec.get("language", "python")
    checks = task_spec.get("assert_checks", [])

    campaign_descriptor = {
        "task_id": task_id,
        "prompt": prompt,
        "language": language,
        "checks": checks,
        "phases": ["generate", "compile", "execute", "record"],
    }

    composition_store.bind(
        source_type="training",
        source_id=task_id,
        target_type="campaign",
        target_id="campaign:%s" % task_id,
        kind="drives",
        metadata={"language": language, "check_count": len(checks)},
    )

    return campaign_descriptor


def fleet_evidence_to_goal(
    fleet_results: list[dict],
    goal_id: str,
) -> dict:
    """Map fleet provenance evidence to goal criteria verification.

    Fleet workers produce provenance-tagged results with objective markers.
    This bridge checks those markers against the goal's success criteria.
    """
    import goal_store
    from sonder_runtime.adapters.persistence import composition_store

    goal = goal_store.get_active()
    if not goal or goal["id"] != goal_id:
        return {"error": "goal not found or not active"}

    criteria = goal.get("criteria", [])
    matched = []
    unmatched = list(criteria)

    for result in fleet_results:
        output = str(result.get("output", "")).lower()
        for criterion in list(unmatched):
            keywords = criterion.lower().split()
            if all(kw in output for kw in keywords[:3]):
                matched.append(criterion)
                unmatched.remove(criterion)

    if matched:
        goal_store.add_note(
            "fleet evidence matched %d/%d criteria: %s"
            % (len(matched), len(criteria), "; ".join(matched[:3]))
        )

    composition_store.bind(
        source_type="fleet",
        source_id="fleet-evidence",
        target_type="goal",
        target_id=goal_id,
        kind="tracks",
        metadata={
            "matched": len(matched),
            "total": len(criteria),
            "unmatched": unmatched[:5],
        },
    )

    return {
        "goal_id": goal_id,
        "criteria_matched": len(matched),
        "criteria_total": len(criteria),
        "matched": matched,
        "unmatched": unmatched,
    }


def preferences_to_emotion_adjustments(preference_lessons: list[dict]) -> dict:
    """Analyze preference lessons and suggest emotion vector adjustments.

    Maps natural language preference patterns to the 19 emotion vector
    dimensions.  Returns suggestions only — the caller decides whether to apply.
    """
    PREFERENCE_VECTOR_MAP = {
        "concise": {"brevity": 0.3, "directness": 0.2},
        "brief": {"brevity": 0.3, "directness": 0.2},
        "short": {"brevity": 0.2},
        "verbose": {"brevity": -0.3},
        "detailed": {"brevity": -0.2, "precision": 0.2},
        "thorough": {"brevity": -0.2, "rigor": 0.2},
        "friendly": {"warmth": 0.3, "empathy": 0.2},
        "warm": {"warmth": 0.3},
        "professional": {"directness": 0.2, "precision": 0.2, "warmth": -0.1},
        "formal": {"directness": 0.2, "playfulness": -0.2},
        "casual": {"playfulness": 0.2, "warmth": 0.2},
        "creative": {"creativity": 0.3, "initiative": 0.2},
        "careful": {"rigor": 0.2, "skepticism": 0.1},
        "bold": {"confidence": 0.2, "initiative": 0.2},
        "patient": {"patience": 0.3, "calm": 0.2},
        "direct": {"directness": 0.3, "brevity": 0.1},
        "encouraging": {"encouragement": 0.3, "warmth": 0.1},
        "precise": {"precision": 0.3, "rigor": 0.2},
        "curious": {"curiosity": 0.3},
        "urgent": {"urgency": 0.3, "brevity": 0.1},
        "humble": {"humility": 0.3},
        "transparent": {"transparency": 0.3},
        "skeptical": {"skepticism": 0.3},
        "calm": {"calm": 0.3, "patience": 0.2},
        "adaptable": {"adaptability": 0.3},
    }

    adjustments = {}
    matched_preferences = []

    for lesson in preference_lessons:
        text = str(lesson.get("text", lesson.get("content", ""))).lower()
        for keyword, vectors in PREFERENCE_VECTOR_MAP.items():
            if keyword in text:
                matched_preferences.append(keyword)
                for vector, delta in vectors.items():
                    adjustments[vector] = adjustments.get(vector, 0.0) + delta

    for vector in adjustments:
        adjustments[vector] = max(-1.0, min(1.0, adjustments[vector]))

    return {
        "adjustments": adjustments,
        "matched_preferences": matched_preferences,
        "applied": False,
    }


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
    "autopilot_outcomes_to_memory",
    "composition_status",
    "fleet_evidence_to_goal",
    "format_composition_status",
    "format_mission_status",
    "goal_to_autopilot",
    "goal_to_plan",
    "mission_start",
    "mission_status",
    "on_autopilot_terminal",
    "on_workflow_complete",
    "plan_to_workflow",
    "preferences_to_emotion_adjustments",
    "selfmod_observations_to_goals",
    "training_to_campaign",
]
