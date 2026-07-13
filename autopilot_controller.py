"""Host-enforced autonomous goal controller.

The controller is request/thread scoped and owns no durable resources. It asks
injected model callbacks for bounded planning/replanning judgment and asks an
injected workbench callback to execute one task at a time. ``autopilot_store``
owns persistence and cross-process control. The host, never the model, decides
which states/actions are legal and whether evidence satisfies completion gates.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

import autopilot_store


TASK_KINDS = ("inspect", "research", "implement", "validate", "report")
POLICIES = ("observe", "workspace")
LOCAL_TIERS = ("fast", "code", "general")
MAX_TOTAL_CYCLES = 50
MAX_ADAPTIVE_CHECKPOINTS = 6
MAX_TASK_OUTPUT = 32_000
FAILURE_PREFIXES = (
    "ERROR:", "VALIDATION_FAILED:", "EVIDENCE_REQUIRED", "CANCELLED",
)


class AutopilotError(RuntimeError):
    pass


@dataclass(frozen=True)
class HostTaskResult:
    """Non-model host observations returned by the guarded workbench."""

    output: str
    tools: tuple[str, ...] = ()
    mutation_observed: bool = False
    validation_attempted: bool = False
    validation_passed: bool = False

    def receipt(self) -> dict:
        return {
            "schema": 1,
            "tools": list(self.tools),
            "mutation_observed": self.mutation_observed,
            "validation_attempted": self.validation_attempted,
            "validation_passed": self.validation_passed,
        }


def normalize_policy(value: str) -> str:
    policy = str(value or "workspace").strip().lower()
    if policy not in POLICIES:
        raise ValueError("policy must be one of: %s" % ", ".join(POLICIES))
    return policy


def normalize_tier(value: str) -> str:
    tier = str(value or "code").strip().lower()
    if tier not in LOCAL_TIERS:
        raise ValueError(
            "autopilot accepts local tiers only: %s" % ", ".join(LOCAL_TIERS)
        )
    return tier


def _clean_text(value, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _task(raw: dict, index: int) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("each autopilot task must be a JSON object")
    title = _clean_text(raw.get("title"), 140)
    instruction = _clean_text(
        raw.get("instruction") or raw.get("description") or raw.get("task"),
        4_000,
    )
    kind = str(raw.get("kind") or "implement").strip().lower()
    if not title or not instruction:
        raise ValueError("each autopilot task needs a title and instruction")
    if kind not in TASK_KINDS:
        raise ValueError("autopilot task kind must be one of: %s" % ", ".join(TASK_KINDS))
    return {
        "id": "task-%02d" % (index + 1),
        "title": title,
        "instruction": instruction,
        "kind": kind,
        "status": "pending",
        "attempts": 0,
        "output": "",
        "error": "",
        "history": [],
    }


def normalize_plan(payload: dict, objective: str, max_tasks: int) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("autopilot planner must return a JSON object")
    criteria_raw = payload.get("success_criteria") or payload.get("criteria") or []
    tasks_raw = payload.get("tasks") or []
    if not isinstance(criteria_raw, list) or not isinstance(tasks_raw, list):
        raise ValueError("autopilot plan criteria and tasks must be JSON lists")
    criteria = []
    for value in criteria_raw:
        text = _clean_text(value, 500)
        if text and text.lower() not in {item.lower() for item in criteria}:
            criteria.append(text)
    if not criteria:
        raise ValueError("autopilot plan needs at least one measurable success criterion")
    max_tasks = max(3, min(int(max_tasks), 24))
    tasks = [_task(raw, index) for index, raw in enumerate(tasks_raw[:max_tasks])]
    if not tasks:
        raise ValueError("autopilot plan needs at least one task")
    if not any(task["kind"] == "validate" for task in tasks):
        if len(tasks) >= max_tasks:
            tasks = tasks[:max_tasks - 1]
        tasks.append(_task({
            "title": "Validate the completed objective",
            "kind": "validate",
            "instruction": (
                "Run grounded checks against the current workspace and verify every "
                "success criterion for this objective: %s" % _clean_text(objective, 1_000)
            ),
        }, len(tasks)))
    seen = set()
    deduped = []
    for task in tasks:
        signature = (task["title"].lower(), task["instruction"].lower())
        if signature in seen:
            continue
        seen.add(signature)
        task["id"] = "task-%02d" % (len(deduped) + 1)
        deduped.append(task)
    if not any(task["kind"] == "validate" for task in deduped):
        raise ValueError("autopilot plan lost its required validation task")
    return {
        "summary": _clean_text(payload.get("summary") or objective, 2_000),
        "criteria": criteria[:12],
        "tasks": deduped,
    }


def normalize_review(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("autopilot review must be a JSON object")
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in ("complete", "continue", "retry", "replan", "pause"):
        raise ValueError("autopilot review decision is invalid")
    tasks = payload.get("tasks") or []
    if not isinstance(tasks, list):
        raise ValueError("autopilot review tasks must be a JSON list")
    assessments = payload.get("pending_assessment") or []
    if not isinstance(assessments, list):
        raise ValueError("autopilot pending assessment must be a JSON list")
    normalized_assessments = []
    for item in assessments:
        if not isinstance(item, dict):
            # Skip malformed entries rather than failing the whole run: this
            # function already silently drops items with a missing/bad verdict
            # just below, and a local reviewer occasionally emits a stray
            # non-object in the list. The host re-derives every pending task's
            # verdict (defaulting to "keep") downstream, so a dropped junk entry
            # changes no decision.
            continue
        task_id = _clean_text(item.get("id"), 80)
        verdict = str(item.get("verdict") or "").strip().lower()
        if task_id and verdict in {"keep", "stale"}:
            normalized_assessments.append({
                "id": task_id,
                "verdict": verdict,
                "reason": _clean_text(item.get("reason"), 500),
            })
    return {
        "decision": decision,
        "reason": _clean_text(payload.get("reason"), 2_000),
        "instruction": _clean_text(payload.get("instruction"), 4_000),
        "tasks": tasks,
        "pending_assessment": normalized_assessments,
    }


def _task_passed(result, task: dict) -> tuple[bool, str]:
    if not isinstance(result, HostTaskResult):
        return False, "workbench returned no host-issued execution receipt"
    text = str(result.output or "").strip()
    if not text:
        return False, "workbench returned an empty result"
    if text.startswith(FAILURE_PREFIXES):
        return False, text.splitlines()[0][:500]
    if not result.tools:
        return False, "workbench returned no host-observed tool evidence"
    if task.get("kind") == "implement" and not result.mutation_observed:
        return False, "implementation task produced no host-observed persistent mutation"
    if task.get("kind") == "validate":
        if not result.validation_attempted:
            return False, "validation task ran no host-observed validator"
        if not result.validation_passed:
            return False, "validation task did not pass host coverage checks"
    return True, ""


def _first_line(value: str, fallback="") -> str:
    return next(
        (line.strip() for line in str(value or "").splitlines() if line.strip()),
        fallback,
    )[:500]


def _evidence_actions(value: str, limit: int = 8) -> list[str]:
    actions = []
    for line in str(value or "").splitlines():
        match = re.match(r"\s*step\s+\d+\s+tool=([A-Za-z0-9_]+)\s+reason=(.*)", line)
        if not match:
            continue
        item = "%s%s" % (
            match.group(1),
            ": " + _clean_text(match.group(2), 240) if match.group(2).strip() else "",
        )
        if item not in actions:
            actions.append(item)
        if len(actions) >= limit:
            break
    return actions


def _completion_gate(run: dict) -> tuple[bool, str]:
    plan = run.get("plan") or []
    if not plan:
        return False, "no plan exists"
    incomplete = [
        task for task in plan if task.get("status") not in ("passed", "superseded")
    ]
    if incomplete:
        return False, "%d plan task(s) are not passed" % len(incomplete)
    validations = [
        task for task in plan
        if task.get("kind") == "validate"
        and task.get("status") == "passed"
        and (task.get("host_receipt") or {}).get("validation_passed") is True
    ]
    if not validations:
        return False, "no validation task passed"
    if not run.get("criteria"):
        return False, "no success criteria were persisted"
    return True, "host completion gates passed"


def _next_pending(plan: list[dict]) -> tuple[int, dict] | tuple[None, None]:
    for index, task in enumerate(plan):
        if task.get("status") == "pending":
            return index, task
    return None, None


def _repair_interrupted_tasks(plan: list[dict]) -> bool:
    changed = False
    for task in plan:
        if task.get("status") == "running":
            task["status"] = "pending"
            task["error"] = "interrupted before a result was committed"
            changed = True
    return changed


def _append_replan(
    run: dict,
    failed_index: int | None,
    raw_tasks: list,
    *,
    supersede_pending: bool = False,
    supersede_ids=(),
) -> list[dict]:
    plan = [dict(task) for task in (run.get("plan") or [])]
    max_tasks = int(run.get("max_tasks") or 12)
    if failed_index is not None and 0 <= failed_index < len(plan):
        failed = dict(plan[failed_index])
        history = list(failed.get("history") or [])
        history.append({
            "instruction": failed.get("instruction", ""),
            "output": _first_line(failed.get("output"), "failed"),
            "error": failed.get("error", ""),
        })
        plan[failed_index]["status"] = "superseded"
        plan[failed_index]["history"] = history[-5:]
    stale_ids = {str(task_id) for task_id in supersede_ids if str(task_id)}
    superseded_count = 0
    if supersede_pending or stale_ids:
        for task in plan:
            if task.get("status") == "pending" and (
                supersede_pending or str(task.get("id")) in stale_ids
            ):
                task["status"] = "superseded"
                task["error"] = "superseded by an evidence-aware checkpoint replan"
                superseded_count += 1
    remaining = max_tasks - len(plan)
    if remaining <= 0 and raw_tasks:
        raise ValueError("autopilot plan budget has no room for replanning")
    if not isinstance(raw_tasks, list):
        raise ValueError("autopilot replan tasks must be a JSON list")
    normalized = []
    signatures = {
        (str(item.get("title", "")).lower(), str(item.get("instruction", "")).lower())
        for item in plan
    }
    for raw in raw_tasks:
        if len(normalized) >= remaining:
            break
        task = _task(raw, len(plan) + len(normalized))
        if run.get("policy") == "observe" and task["kind"] == "implement":
            raise ValueError("observe policy cannot accept implementation replan tasks")
        signature = (task["title"].lower(), task["instruction"].lower())
        if signature in signatures:
            continue
        signatures.add(signature)
        normalized.append(task)
    has_validation = any(
        task.get("kind") == "validate" and task.get("status") != "superseded"
        for task in (*plan, *normalized)
    )
    if not has_validation:
        validation = _task({
            "title": "Validate the replanned objective",
            "kind": "validate",
            "instruction": (
                "Run grounded checks against the current workspace and verify every "
                "persisted success criterion for this objective: %s"
                % _clean_text(run.get("objective", ""), 1_000)
            ),
        }, len(plan) + len(normalized))
        if remaining <= 0:
            raise ValueError("autopilot plan budget has no room for required validation")
        if len(normalized) >= remaining:
            normalized[-1] = validation
        else:
            normalized.append(validation)
    if not normalized and not superseded_count:
        raise ValueError("autopilot replan produced no unique tasks")
    for task in normalized:
        task["id"] = "task-%02d" % (len(plan) + 1)
        plan.append(task)
    return plan


def format_report(run: dict, review_reason: str = "") -> str:
    lines = [
        "autopilot end report",
        "  run: %s" % run.get("id", ""),
        "  objective: %s" % run.get("objective", ""),
        "  policy/tier: %s / %s" % (run.get("policy", ""), run.get("tier", "")),
        "  cycles/failures: %s/%s" % (run.get("cycles", 0), run.get("failures", 0)),
        "  adaptive/checkpoints/replans: %s / %s / %s/%s" % (
            "on" if run.get("adaptive", True) else "off",
            run.get("checkpoints", 0),
            run.get("replans", 0),
            run.get("max_replans", 0),
        ),
        "  success criteria:",
    ]
    lines.extend("    - %s" % item for item in (run.get("criteria") or []))
    lines.append("  task ledger:")
    for task in run.get("plan") or []:
        lines.append("    - %(id)s [%(status)s] %(kind)s: %(title)s" % task)
        if task.get("output"):
            lines.append("        outcome: %s" % _first_line(task["output"]))
            for action in _evidence_actions(task["output"]):
                lines.append("        action: %s" % action)
        if task.get("error"):
            lines.append("        error: %s" % _first_line(task["error"]))
    if review_reason:
        lines.append("  reviewer: %s" % review_reason)
    return "\n".join(lines)


def format_run(run: dict | None, include_report: bool = True) -> str:
    if not run:
        return "(no autopilot run)"
    plan = run.get("plan") or []
    passed = sum(1 for task in plan if task.get("status") == "passed")
    pending = sum(1 for task in plan if task.get("status") == "pending")
    lines = [
        "sonder autopilot",
        "  id: %s" % run.get("id", ""),
        "  status/phase: %s / %s" % (run.get("status", ""), run.get("phase", "")),
        "  objective: %s" % run.get("objective", ""),
        "  policy/tier/web: %s / %s / %s" % (
            run.get("policy", ""), run.get("tier", ""),
            "on" if run.get("allow_web") else "off",
        ),
        "  tasks: %d passed, %d pending, %d total" % (passed, pending, len(plan)),
        "  cycles/failures: %s/%s" % (run.get("cycles", 0), run.get("failures", 0)),
        "  adaptive: %s | checkpoints: %s | replans: %s/%s" % (
            "on" if run.get("adaptive", True) else "off",
            run.get("checkpoints", 0),
            run.get("replans", 0),
            run.get("max_replans", 0),
        ),
    ]
    if run.get("summary"):
        lines.append("  summary: %s" % run["summary"])
    for task in plan[:12]:
        lines.append("  - %(id)s [%(status)s] %(kind)s: %(title)s" % task)
    if include_report and run.get("final_report"):
        lines.extend(["", run["final_report"]])
    return "\n".join(lines)


def snapshot(include_finished: bool = True, limit: int = 20) -> dict:
    data = autopilot_store.snapshot(include_finished=include_finished, limit=limit)
    latest = data.get("latest")
    data["events"] = (
        autopilot_store.events(latest["id"], limit=12) if latest else []
    )
    return data


def format_snapshot(data: dict) -> str:
    lines = [
        "autopilot controller status",
        "  active: %s | resumable: %s | listed: %s" % (
            data.get("active_runs", 0), data.get("resumable_runs", 0),
            data.get("total_listed", 0),
        ),
        "  persistence: %s" % data.get("database", ""),
    ]
    rows = data.get("runs") or []
    if not rows:
        lines.append("  runs: none yet")
    for run in rows[:10]:
        lines.append("  - %(id)s [%(status)s/%(phase)s] %(objective)s" % run)
        lines.append("      cycles=%s failures=%s replans=%s/%s policy=%s" % (
            run.get("cycles", 0), run.get("failures", 0),
            run.get("replans", 0), run.get("max_replans", 0),
            run.get("policy", ""),
        ))
    return "\n".join(lines)


def execute_run(
    run_id: str,
    owner_id: str,
    *,
    owner_pid: int,
    plan_fn: Callable[[dict], dict],
    work_fn: Callable[[dict, dict, str], str],
    review_fn: Callable[[dict, str], dict],
    max_cycles: int = 6,
    plan_only: bool = False,
) -> dict:
    """Claim and advance one run on the caller thread.

    ``plan_fn`` and ``review_fn`` are model-judgment boundaries. ``work_fn`` is
    expected to call the guarded workbench. Every transition, budget, evidence
    gate, and terminal status remains deterministic host logic.
    """
    max_cycles = max(1, min(int(max_cycles or 6), 12))
    run = autopilot_store.claim_run(
        run_id, owner_id, owner_pid=owner_pid,
    )
    if not run:
        raise AutopilotError("run is unavailable, terminal, cancelled, or owned elsewhere")
    try:
        plan = [dict(task) for task in (run.get("plan") or [])]
        if _repair_interrupted_tasks(plan):
            run = autopilot_store.save_progress(
                run["id"], owner_id, plan=plan, status="running", phase="execute",
                event_kind="resume", event_message="interrupted task returned to pending",
            ) or run
        if not plan:
            proposed = normalize_plan(
                plan_fn(run), run["objective"], run.get("max_tasks") or 12,
            )
            flags = autopilot_store.control_flags(run["id"], owner_id)
            if flags.get("lost"):
                raise AutopilotError("autopilot ownership was lost during planning")
            if flags.get("cancel"):
                return autopilot_store.finish_run(
                    run["id"], owner_id, "cancelled",
                    summary="cancelled while the plan was being prepared",
                ) or run
            plan = proposed["tasks"]
            run = autopilot_store.save_progress(
                run["id"], owner_id,
                plan=plan,
                criteria=proposed["criteria"],
                plan_summary=proposed["summary"],
                status="running",
                phase="execute",
                event_kind="planned",
                event_message="model plan accepted by host schema and budgets",
            ) or run
        if plan_only:
            report = format_report(run, "plan created; execution not requested")
            return autopilot_store.finish_run(
                run["id"], owner_id, "paused",
                summary="plan ready; explicit resume will execute it",
                final_report=report,
            ) or run

        invoked_cycles = 0
        while invoked_cycles < max_cycles:
            flags = autopilot_store.control_flags(run["id"], owner_id)
            if flags.get("lost"):
                raise AutopilotError("autopilot ownership was lost")
            if flags.get("cancel"):
                return autopilot_store.finish_run(
                    run["id"], owner_id, "cancelled",
                    summary="cancelled at a host checkpoint",
                ) or run
            if flags.get("pause"):
                return autopilot_store.finish_run(
                    run["id"], owner_id, "paused",
                    summary="paused at a host checkpoint",
                ) or run
            run = autopilot_store.get_run(run["id"]) or run
            if int(run.get("cycles") or 0) >= MAX_TOTAL_CYCLES:
                return autopilot_store.finish_run(
                    run["id"], owner_id, "blocked",
                    summary="hard autonomous cycle ceiling reached",
                    last_error="maximum total cycles=%d" % MAX_TOTAL_CYCLES,
                ) or run
            plan = [dict(task) for task in (run.get("plan") or [])]
            task_index, task = _next_pending(plan)
            if task is None:
                gate_ok, gate_reason = _completion_gate(run)
                review = normalize_review(review_fn(run, gate_reason))
                flags = autopilot_store.control_flags(run["id"], owner_id)
                if flags.get("lost"):
                    raise AutopilotError("autopilot ownership was lost during review")
                if flags.get("cancel"):
                    return autopilot_store.finish_run(
                        run["id"], owner_id, "cancelled",
                        summary="cancelled during completion review",
                    ) or run
                if flags.get("pause"):
                    return autopilot_store.finish_run(
                        run["id"], owner_id, "paused",
                        summary="paused during completion review",
                    ) or run
                if review["decision"] == "complete" and gate_ok:
                    report = format_report(run, review["reason"] or gate_reason)
                    return autopilot_store.finish_run(
                        run["id"], owner_id, "completed",
                        summary="objective completed with host-verified task evidence",
                        final_report=report,
                    ) or run
                if review["decision"] == "replan" and review["tasks"]:
                    if int(run.get("replans") or 0) >= int(run.get("max_replans") or 0):
                        return autopilot_store.finish_run(
                            run["id"], owner_id, "blocked",
                            summary="review requested replanning but the replan budget is exhausted",
                            last_error="maximum replans=%s" % run.get("max_replans", 0),
                        ) or run
                    try:
                        plan = _append_replan(run, None, review["tasks"])
                    except ValueError as exc:
                        return autopilot_store.finish_run(
                            run["id"], owner_id, "blocked",
                            summary="review requested replanning but task budget is exhausted",
                            last_error=str(exc),
                        ) or run
                    run = autopilot_store.save_progress(
                        run["id"], owner_id, plan=plan, phase="execute",
                        replans_delta=1,
                        event_kind="replan", event_message=review["reason"] or "review added tasks",
                    ) or run
                    continue
                return autopilot_store.finish_run(
                    run["id"], owner_id, "paused",
                    summary=(
                        review["reason"] or
                        ("completion gate denied: %s" % gate_reason)
                    ),
                    final_report=format_report(run, review["reason"]),
                ) or run

            task["status"] = "running"
            task["attempts"] = int(task.get("attempts") or 0) + 1
            plan[task_index] = task
            run = autopilot_store.save_progress(
                run["id"], owner_id, plan=plan, status="running", phase="execute",
                current_task=task_index,
                event_kind="task_start",
                event_message="%s: %s" % (task["id"], task["title"]),
            ) or run
            prior = "\n".join(
                "- %s [%s]: %s" % (
                    item.get("title", ""), item.get("status", ""),
                    _first_line(item.get("output"), item.get("error", "")),
                )
                for item in plan[:task_index]
            )
            result = work_fn(run, task, prior)
            output = str(
                result.output if isinstance(result, HostTaskResult) else result or ""
            )[:MAX_TASK_OUTPUT]
            passed, error = _task_passed(result, task)
            flags = autopilot_store.control_flags(run["id"], owner_id)
            if flags.get("lost"):
                raise AutopilotError("autopilot ownership was lost during task execution")
            if flags.get("cancel"):
                return autopilot_store.finish_run(
                    run["id"], owner_id, "cancelled",
                    summary="cancelled; active task result discarded",
                ) or run
            task["status"] = "passed" if passed else "failed"
            task["output"] = output
            task["error"] = error
            task["host_receipt"] = (
                result.receipt() if isinstance(result, HostTaskResult) else {}
            )
            plan[task_index] = task
            run = autopilot_store.save_progress(
                run["id"], owner_id,
                plan=plan,
                cycles_delta=1,
                failures_delta=0 if passed else 1,
                current_task=-1,
                event_kind="task_pass" if passed else "task_fail",
                event_message=(
                    "%s passed" % task["id"] if passed
                    else "%s failed: %s" % (task["id"], error)
                ),
            ) or run
            invoked_cycles += 1
            if passed:
                pending_index, _pending_task = _next_pending(run.get("plan") or [])
                should_checkpoint = (
                    bool(run.get("adaptive", True))
                    and task.get("kind") in ("inspect", "research")
                    and pending_index is not None
                    and int(run.get("checkpoints") or 0) < MAX_ADAPTIVE_CHECKPOINTS
                )
                if should_checkpoint:
                    flags = autopilot_store.control_flags(run["id"], owner_id)
                    if flags.get("lost"):
                        raise AutopilotError(
                            "autopilot ownership was lost before adaptive review"
                        )
                    if flags.get("cancel"):
                        return autopilot_store.finish_run(
                            run["id"], owner_id, "cancelled",
                            summary="cancelled before adaptive review",
                        ) or run
                    if flags.get("pause"):
                        return autopilot_store.finish_run(
                            run["id"], owner_id, "paused",
                            summary="paused before adaptive review",
                        ) or run
                    checkpoint_reason = (
                        "adaptive checkpoint after %s passed; inspect the new evidence "
                        "and decide whether the remaining pending plan is still correct"
                        % task.get("id", "discovery task")
                    )
                    review = normalize_review(review_fn(run, checkpoint_reason))
                    flags = autopilot_store.control_flags(run["id"], owner_id)
                    if flags.get("lost"):
                        raise AutopilotError(
                            "autopilot ownership was lost during adaptive review"
                        )
                    if flags.get("cancel"):
                        return autopilot_store.finish_run(
                            run["id"], owner_id, "cancelled",
                            summary="cancelled during adaptive review",
                        ) or run
                    if flags.get("pause"):
                        run = autopilot_store.save_progress(
                            run["id"], owner_id, checkpoints_delta=1,
                            event_kind="checkpoint_pause",
                            event_message="operator pause arrived during adaptive review",
                        ) or run
                        return autopilot_store.finish_run(
                            run["id"], owner_id, "paused",
                            summary="paused during adaptive review",
                            final_report=format_report(run, review["reason"]),
                        ) or run
                    stale_ids = {
                        item["id"]
                        for item in review.get("pending_assessment") or []
                        if item.get("verdict") == "stale"
                    }
                    if review["decision"] == "replan" and (
                        review["tasks"] or stale_ids
                    ):
                        if int(run.get("replans") or 0) >= int(
                            run.get("max_replans") or 0
                        ):
                            run = autopilot_store.save_progress(
                                run["id"], owner_id, checkpoints_delta=1,
                                event_kind="checkpoint_blocked",
                                event_message="adaptive reviewer requested a replan after the budget was exhausted",
                            ) or run
                            return autopilot_store.finish_run(
                                run["id"], owner_id, "blocked",
                                summary="adaptive replan budget exhausted",
                                last_error="maximum replans=%s"
                                % run.get("max_replans", 0),
                                final_report=format_report(run, review["reason"]),
                            ) or run
                        try:
                            plan = _append_replan(
                                run,
                                None,
                                review["tasks"],
                                supersede_pending=not bool(stale_ids),
                                supersede_ids=stale_ids,
                            )
                        except ValueError as exc:
                            run = autopilot_store.save_progress(
                                run["id"], owner_id, checkpoints_delta=1,
                                event_kind="checkpoint_blocked",
                                event_message=str(exc),
                            ) or run
                            return autopilot_store.finish_run(
                                run["id"], owner_id, "blocked",
                                summary="adaptive replanning failed host validation",
                                last_error=str(exc),
                                final_report=format_report(run, review["reason"]),
                            ) or run
                        run = autopilot_store.save_progress(
                            run["id"], owner_id,
                            plan=plan,
                            checkpoints_delta=1,
                            replans_delta=1,
                            event_kind="adaptive_replan",
                            event_message=review["reason"] or (
                                "new evidence replaced the remaining pending plan"
                            ),
                        ) or run
                        continue
                    run = autopilot_store.save_progress(
                        run["id"], owner_id,
                        checkpoints_delta=1,
                        event_kind=(
                            "checkpoint_pause"
                            if review["decision"] == "pause"
                            else "checkpoint_continue"
                        ),
                        event_message=review["reason"] or (
                            "remaining plan retained after evidence review"
                        ),
                    ) or run
                    if review["decision"] == "pause":
                        return autopilot_store.finish_run(
                            run["id"], owner_id, "paused",
                            summary=review["reason"] or "paused by adaptive reviewer",
                            final_report=format_report(run, review["reason"]),
                        ) or run
                continue
            if int(run.get("failures") or 0) >= int(run.get("max_failures") or 3):
                return autopilot_store.finish_run(
                    run["id"], owner_id, "blocked",
                    summary="failure budget exhausted",
                    last_error=error,
                    final_report=format_report(run, error),
                ) or run
            review = normalize_review(review_fn(run, error))
            flags = autopilot_store.control_flags(run["id"], owner_id)
            if flags.get("lost"):
                raise AutopilotError("autopilot ownership was lost during failure review")
            if flags.get("cancel"):
                return autopilot_store.finish_run(
                    run["id"], owner_id, "cancelled",
                    summary="cancelled during failure review",
                ) or run
            if flags.get("pause"):
                return autopilot_store.finish_run(
                    run["id"], owner_id, "paused",
                    summary="paused during failure review",
                    last_error=error,
                    final_report=format_report(run, review["reason"]),
                ) or run
            if review["decision"] == "retry" and task["attempts"] < 2:
                task["status"] = "pending"
                task["instruction"] = review["instruction"] or task["instruction"]
                plan[task_index] = task
                run = autopilot_store.save_progress(
                    run["id"], owner_id, plan=plan,
                    event_kind="retry", event_message=review["reason"] or "task retry approved",
                ) or run
                continue
            if review["decision"] == "replan" and review["tasks"]:
                if int(run.get("replans") or 0) >= int(run.get("max_replans") or 0):
                    return autopilot_store.finish_run(
                        run["id"], owner_id, "blocked",
                        summary="failure review requested replanning but the budget is exhausted",
                        last_error="maximum replans=%s" % run.get("max_replans", 0),
                        final_report=format_report(run, review["reason"]),
                    ) or run
                try:
                    plan = _append_replan(run, task_index, review["tasks"])
                except ValueError as exc:
                    return autopilot_store.finish_run(
                        run["id"], owner_id, "blocked",
                        summary="replanning failed host validation",
                        last_error=str(exc),
                    ) or run
                run = autopilot_store.save_progress(
                    run["id"], owner_id, plan=plan,
                    replans_delta=1,
                    event_kind="replan", event_message=review["reason"] or "failed task replanned",
                ) or run
                continue
            return autopilot_store.finish_run(
                run["id"], owner_id, "paused" if review["decision"] == "pause" else "blocked",
                summary=review["reason"] or "model requested operator review",
                last_error=error,
                final_report=format_report(run, review["reason"]),
            ) or run

        run = autopilot_store.get_run(run["id"]) or run
        plan = run.get("plan") or []
        if _next_pending(plan)[0] is None:
            gate_ok, gate_reason = _completion_gate(run)
            review = normalize_review(review_fn(run, gate_reason))
            flags = autopilot_store.control_flags(run["id"], owner_id)
            if flags.get("lost"):
                raise AutopilotError("autopilot ownership was lost during final review")
            if flags.get("cancel"):
                return autopilot_store.finish_run(
                    run["id"], owner_id, "cancelled",
                    summary="cancelled during final review",
                ) or run
            if review["decision"] == "complete" and gate_ok:
                return autopilot_store.finish_run(
                    run["id"], owner_id, "completed",
                    summary="objective completed with host-verified task evidence",
                    final_report=format_report(run, review["reason"] or gate_reason),
                ) or run
        return autopilot_store.finish_run(
            run["id"], owner_id, "paused",
            summary="per-invocation autonomous cycle budget reached; resume to continue",
            final_report=format_report(run),
        ) or run
    except Exception as exc:
        latest = autopilot_store.get_run(run["id"]) or run
        stored = autopilot_store.finish_run(
            run["id"], owner_id, "failed",
            summary="autopilot controller failed safely",
            last_error=str(exc),
            final_report=format_report(latest, str(exc)),
        )
        if stored:
            return stored
        raise
