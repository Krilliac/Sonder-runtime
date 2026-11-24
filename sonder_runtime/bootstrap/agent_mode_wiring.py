"""Composition-root wiring for secondary agent/autopilot modes and hosted session export."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from sonder_runtime.domain import agent_modes
from sonder_runtime.domain.cloud_access import LEGACY_ERROR_PREFIX
from sonder_runtime.domain.session_ref import hosted_session_storage_id


parse_agent_mode_arg = agent_modes.parse_agent_mode_arg


def prepare_agent_mode_run(
    arg: str,
    *,
    fanout_fn: Callable[..., str],
    request_owner: str = "local-operator",
    request_role: str = "local-open",
    allow_fanout: bool = True,
) -> tuple[agent_modes.ParsedAgentMode, str]:
    """Parse ``/work`` / ``/agent`` mode prefixes and execute host-owned local fanout preflight."""
    parsed = agent_modes.parse_agent_mode_arg(arg)
    if parsed.usage_error:
        return parsed, parsed.usage_error
    if parsed.mode == "fanout":
        if not allow_fanout:
            return (
                parsed,
                f"{LEGACY_ERROR_PREFIX} HOST POLICY: /agent fanout requires developer authorization.",
            )
        receipt = fanout_fn(
            parsed.task,
            scope="local",
            num_predict=agent_modes.AGENT_FANOUT_DEFAULT_NUM_PREDICT,
            timeout=60,
            max_cloud_workers=1,
            request_owner=request_owner,
            request_role=request_role,
        )
        return (
            agent_modes.ParsedAgentMode(
                mode=parsed.mode,
                task=parsed.task,
                prompt=agent_modes.format_fanout_preflight_prompt(parsed.task, receipt),
                max_steps=parsed.max_steps,
            ),
            "",
        )
    return parsed, ""


def dispatch_agent_ensemble(
    args: Mapping[str, Any],
    *,
    is_cloud_tier: Callable[[str], bool],
    ensemble_fn: Callable[..., str],
    unsafe: bool = False,
) -> str:
    """Validate and execute a local-only ``ensemble_answer`` call from an agent turn."""
    validated, err = agent_modes.validate_agent_ensemble_args(
        args, is_cloud_tier=is_cloud_tier, unsafe=unsafe,
    )
    if err or validated is None:
        return err
    return ensemble_fn(
        prompt=validated["prompt"],
        tiers=validated["tiers"],
        synth_tier=validated["synth_tier"],
        num_predict=validated["num_predict"],
        mode=validated["mode"],
        project=validated["project"],
    )


def dispatch_agent_consult(
    args: Mapping[str, Any],
    *,
    is_cloud_tier: Callable[[str], bool],
    consult_flow: Any,
    ensemble_fn: Callable[..., str],
    lookup_scope_fn: Callable[[], Any],
    unsafe: bool = False,
) -> str:
    """Validate and execute a local-only ``consult`` call from an agent turn."""
    validated, err = agent_modes.validate_agent_consult_args(
        args, is_cloud_tier=is_cloud_tier, unsafe=unsafe,
    )
    if err or validated is None:
        return err
    chosen = [t.strip() for t in str(validated["tiers"]).split(",") if t.strip()] or None
    result = consult_flow.consult(
        validated["prompt"],
        chosen,
        ask_fn=lambda question, tier: ensemble_fn(question, tiers=tier, mode="general"),
        cloud_ok=False,
        lookup=lookup_scope_fn(),
    )
    return str(consult_flow.format_result(result))


def build_autopilot_mode_objective(
    strategy_mode: str,
    objective: str,
    *,
    fanout_fn: Callable[..., str],
    request_owner: str = "",
) -> str:
    """Apply an autopilot secondary strategy directive (and local fanout preflight when requested)."""
    if not strategy_mode:
        return objective
    if strategy_mode == "fanout":
        receipt = fanout_fn(
            objective,
            scope="local",
            num_predict=agent_modes.AGENT_FANOUT_DEFAULT_NUM_PREDICT,
            timeout=60,
            max_cloud_workers=1,
            request_owner=request_owner or "local-operator",
            request_role="autopilot",
        )
        return agent_modes.format_fanout_preflight_prompt(objective, receipt)
    return agent_modes.apply_agent_mode(strategy_mode, objective)


def run_autopilot_command(
    arg: str,
    *,
    project: str = "",
    request_owner: str | None = None,
    parse_run_args: Callable[[str, str], Any],
    help_text: str,
    resolve_project: Callable[[str], str],
    autopilot_status: Callable[..., str],
    autopilot_start: Callable[..., str],
    autopilot_steer: Callable[..., str],
    autopilot_resume: Callable[..., str],
    autopilot_pause: Callable[..., str],
    autopilot_cancel: Callable[..., str],
    launch_autopilot: Callable[[str], bool],
    autopilot_not_launched: Callable[[str], str],
    goal_to_autopilot: Callable[..., Mapping[str, Any]],
    active_goal_fn: Callable[[], Mapping[str, Any] | None],
    fanout_fn: Callable[..., str],
) -> str:
    """Dispatch ``/autopilot`` actions including secondary multi-model/fleet modes."""
    text = str(arg or "").strip()
    if not text:
        return autopilot_status(request_owner=request_owner)
    action, _, rest = text.partition(" ")
    action = action.lower()
    rest = rest.strip()
    if action in ("status", "show", "list"):
        return autopilot_status(rest, request_owner=request_owner)
    strategy_mode = agent_modes.normalize_agent_mode(action)
    if action in ("run", "start", "plan") or strategy_mode:
        parsed = parse_run_args(action, rest)
        if parsed.error_detail:
            return f"{LEGACY_ERROR_PREFIX} {parsed.error_detail}"
        if parsed.usage_error:
            return str(parsed.usage_error)
        if strategy_mode and parsed.policy == "observe":
            return f"{LEGACY_ERROR_PREFIX} /autopilot {strategy_mode} requires workspace mode (remove --observe)."
        if parsed.bind_goal:
            active = active_goal_fn()
            if not active:
                return "--goal requires an active goal (use /goal set first)."
            goal_payload = dict(active)
            if strategy_mode:
                goal_payload["objective"] = build_autopilot_mode_objective(
                    strategy_mode,
                    str(goal_payload.get("objective") or ""),
                    fanout_fn=fanout_fn,
                    request_owner=request_owner or "",
                )
            ap_result = goal_to_autopilot(
                goal_payload,
                policy=parsed.policy,
                allow_web=parsed.allow_web,
                project=resolve_project(project) or "",
                request_owner=request_owner or "",
            )
            if ap_result.get("error"):
                return f"autopilot request failed: {ap_result['error']}"
            run_id = str(ap_result["run_id"])
            launched = launch_autopilot(run_id)
            prefix = f"autopilot bound to goal {active['id']}"
            if not launched:
                prefix = autopilot_not_launched(run_id)
            return (
                f"{prefix}\n  run: {run_id}\n  objective: {str(active['objective'])[:120]}\n"
                f"  use /autopilot status {run_id}"
            )
        objective = build_autopilot_mode_objective(
            strategy_mode,
            parsed.objective,
            fanout_fn=fanout_fn,
            request_owner=request_owner or "",
        )
        return autopilot_start(
            objective=objective,
            project=resolve_project(project) or "",
            request_owner=request_owner or "",
            policy=parsed.policy,
            allow_web=parsed.allow_web,
            adaptive=parsed.adaptive,
            plan_only=action == "plan",
        )
    if action in ("steer", "clarify"):
        run_selector, _, message = rest.partition(" ")
        message = message.strip()
        if not run_selector or not message:
            return f"usage: /autopilot {action} <run-id> <message>"
        return autopilot_steer(
            run_selector,
            message,
            kind="clarify" if action == "clarify" else "guidance",
            request_owner=request_owner,
        )
    if action == "resume":
        return autopilot_resume(rest, request_owner=request_owner) if rest else "usage: /autopilot resume <run-id>"
    if action == "pause":
        return autopilot_pause(rest, request_owner=request_owner) if rest else "usage: /autopilot pause <run-id>"
    if action == "cancel":
        return autopilot_cancel(rest, request_owner=request_owner) if rest else "usage: /autopilot cancel <run-id>"
    if action in ("help", "?"):
        return help_text
    return f"{LEGACY_ERROR_PREFIX} unknown autopilot action '{action}'; try /autopilot help."


def resolve_hosted_session_for_export(
    conn: Any,
    session_id: str,
    *,
    get_session: Callable[[Any, str], Any],
    fuller_turns: Callable[[Callable[[], Any], str, list[Any]], Any],
    get_repository: Callable[[], Any],
) -> tuple[str, Any]:
    """Resolve a raw client session id against account/api-key hosted storage ids and event logs."""
    principals = ["api-key", "local-open"]
    try:
        rows = conn.execute("SELECT username FROM accounts").fetchall()
        for row in rows:
            uname = str(row[0] or "").strip()
            if uname:
                principals.append("account:" + uname)
    except Exception:
        pass
    candidates = [session_id]
    for principal in principals:
        scoped = hosted_session_storage_id(principal, session_id)
        if scoped and scoped not in candidates:
            candidates.append(scoped)
    for candidate in candidates[1:]:
        matched = get_session(conn, candidate)
        if matched is not None:
            return candidate, matched
    for candidate in candidates:
        event_turns = fuller_turns(get_repository, candidate, [])
        if event_turns:
            return candidate, {"session_id": candidate, "title": "", "project": ""}
    return session_id, None


__all__ = [
    "build_autopilot_mode_objective",
    "dispatch_agent_consult",
    "dispatch_agent_ensemble",
    "parse_agent_mode_arg",
    "prepare_agent_mode_run",
    "resolve_hosted_session_for_export",
    "run_autopilot_command",
]
