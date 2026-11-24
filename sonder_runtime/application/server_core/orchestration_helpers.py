"""Pure orchestration, work-routing, web-chat, and chat-payload helpers."""
from __future__ import annotations

from collections.abc import Container
from typing import Any, Callable, Mapping

ORCHESTRATE_MODE_ALIASES: Mapping[str, str] = {
    "delagte": "delegate",
    "delegte": "delegate",
    "paralell": "parallel",
    "inlne": "inline",
    "workflow": "fleet",
}

_FLEET_PROMOTABLE_MODES = frozenset(
    {"ask", "choose", "prompt", "delegate", "delegated", "agents", "parallel"}
)


def normalize_orchestrate_mode(mode: str, *, fleet_requested: bool) -> str:
    """Normalize typos/aliases and promote to fleet mode when requested."""
    normalized = (mode or "ask").strip().lower()
    normalized = ORCHESTRATE_MODE_ALIASES.get(normalized, normalized)
    if fleet_requested and normalized in _FLEET_PROMOTABLE_MODES:
        return "fleet"
    return normalized


def resolve_worker_cap(
    worker_cap: Any,
    *,
    inferred_worker_cap: int | None,
    agents: int,
    capacity_fn: Callable[..., Mapping[str, Any]],
) -> tuple[int | None, int, str]:
    """Validate and resolve worker_cap and default agent count.

    Returns ``(resolved_worker_cap, resolved_agents, error_detail)``.
    """
    raw_worker_cap = worker_cap
    if (worker_cap is None or worker_cap == 0) and not isinstance(worker_cap, bool):
        worker_cap = inferred_worker_cap
    if worker_cap is not None and worker_cap != 0 and not isinstance(raw_worker_cap, bool):
        cap_probe = capacity_fn(agents or None, worker_cap=worker_cap)
        if cap_probe.get("worker_cap_error"):
            return None, agents, "invalid worker_cap: %s" % cap_probe["worker_cap_error"]
        resolved_cap = int(cap_probe["requested_worker_cap"])
        return resolved_cap, (agents or resolved_cap), ""
    if isinstance(raw_worker_cap, bool):
        return None, agents, "invalid worker_cap: boolean values are not worker counts"
    return None, agents, ""


def format_fleet_started_report(
    result: Mapping[str, Any],
    *,
    task: str,
    needs_repo_tools: bool,
    format_plan_fn: Callable[..., str],
) -> str:
    """Format the startup summary for a background fleet run."""
    agent_count = len(result.get("agents") or ())
    worker_slots = int(result.get("worker_slots", 1))
    master_id = str(result["master_id"])
    return "\n".join(
        [
            "master orchestration started",
            "mode: fleet | master=%s | agents=%d" % (master_id, agent_count),
            "worker slots: %d (bounded concurrent model calls)" % worker_slots,
            format_plan_fn(
                task,
                agent_count,
                worker_slots,
                greenfield=not needs_repo_tools,
                output_workspace=result.get("output_workspace", ""),
            ),
            "monitor: master_status() | cancel: master_cancel('%s')" % master_id,
        ]
    )


def format_delegated_complete_report(
    result: Mapping[str, Any],
    *,
    mode: str,
) -> str:
    """Format the completion summary for a synchronous delegated orchestration run."""
    lines = [
        "master orchestration complete",
        "mode: %s | master=%s | agents=%d"
        % (
            "fleet" if mode in ("fleet", "swarm", "fanout") else "delegated",
            result["master_id"],
            len(result.get("agents") or ()),
        ),
        "worker slots used: %d (bounded concurrent model calls)"
        % result.get("worker_slots", 1),
        "",
        str(result["output"]),
    ]
    return "\n".join(lines).strip()


def validate_npu_route_decision(
    npu_decision: Any,
    *,
    valid_modes: Container[str],
    valid_tiers: Container[str],
) -> dict[str, Any] | None:
    """Allowlist-validate an advisory NPU routing pre-score."""
    if not isinstance(npu_decision, dict):
        return None
    candidate_mode = str(npu_decision.get("mode") or "").strip().lower()
    candidate_tier = str(npu_decision.get("tier") or "").strip().lower()
    candidate_reason = npu_decision.get("reason")
    candidate_confidence = npu_decision.get("confidence")
    if (
        candidate_mode in valid_modes
        and candidate_tier in valid_tiers
        and isinstance(candidate_reason, str)
        and bool(candidate_reason.strip())
        and not isinstance(candidate_confidence, bool)
        and isinstance(candidate_confidence, (int, float))
        and 0.0 <= float(candidate_confidence) <= 1.0
    ):
        return {
            "mode": candidate_mode,
            "reason": candidate_reason.strip()[:240],
            "confidence": float(candidate_confidence),
        }
    return None


def format_deferred_autopilot_reply(
    *,
    header: str,
    current_run: Mapping[str, Any],
) -> str:
    """Format the reply when automatic routing defers to an already-active Autopilot run."""
    return "%s\n\nactive run: %s [%s] %s\nuse /autopilot status %s" % (
        header,
        current_run.get("id", "unknown"),
        current_run.get("status", "unknown"),
        current_run.get("objective", ""),
        current_run.get("id", ""),
    )


def format_routed_work_reply(
    *,
    header: str,
    mode: str,
    ignored_worker_cue: str,
    output: str,
) -> str:
    """Assemble the routed work reply with optional ignored-worker-cue note."""
    note = ("\nnote: " + ignored_worker_cue) if ignored_worker_cue and mode != "fleet" else ""
    return "%s%s\n\n%s" % (header, note, output)


def web_capability_reply(web_enabled: bool) -> str:
    """Return the direct capability answer for web-access inquiries."""
    if web_enabled:
        return (
            "Yes. Live public web search, page fetch, and weather tools are enabled. "
            "Ask me to search the web or give me a city/state or ZIP for weather."
        )
    return (
        "This Sonder build has web tools, but they are disabled in the current "
        "runtime by SONDER_WEB_TOOLS."
    )


def build_web_research_task(
    prompt: str,
    intent: Mapping[str, Any],
    *,
    build_search_query: Callable[[str, str], str],
    location_label: Callable[[Any], str],
    location: Any,
) -> str:
    """Construct the live web-research task prompt from a classified web intent."""
    query = str(intent.get("query", prompt))
    task = query
    search_query = build_search_query(query, str(intent.get("kind", "research")))
    if search_query and search_query != query:
        task = (
            "Answer this question using live web results: %s\n"
            "Suggested web_search query (conversational filler already "
            "removed; use it or refine it): %s" % (query, search_query)
        )
    if intent.get("needs_location"):
        task = (
            "%s\n\nThe user explicitly enabled approximate IP location. Their "
            "approximate city/region is %s. Use only that place label, disclose that "
            "it may be inaccurate, and do not claim precise location."
            % (task, location_label(location))
        )
    return task


def apply_turn_effort_to_payload(
    payload: dict[str, Any],
    *,
    active_effort: str | None,
    bridged_rung: Any,
    cloud: bool,
    single_send: bool,
    is_thinking_model: Callable[[str], bool],
    think_supported: Callable[[str], bool],
    model: str,
) -> dict[str, Any]:
    """Attach active reasoning effort and compatible ``think`` toggle to chat payloads."""
    if active_effort is None or not isinstance(payload, dict):
        return payload
    opts = (
        dict(payload.get("options") or {})
        if isinstance(payload.get("options"), dict)
        else {}
    )
    if "reasoning_effort" not in opts:
        payload = dict(payload)
        opts["reasoning_effort"] = active_effort
        payload["options"] = opts
    if bridged_rung is None and not cloud and not single_send and "think" not in payload:
        if active_effort == "off":
            payload = dict(payload)
            payload["think"] = False
        elif is_thinking_model(model) and think_supported(model):
            payload = dict(payload)
            payload["think"] = True
    return payload


def format_traced_reply(
    response: str,
    *,
    footer_iid: str | None,
    trace: bool,
    trace_ctx: Any,
    model: str,
    tier_label: str,
    temperature: Any,
    sampling_desc: str,
    num_predict: int,
    num_ctx: Any,
    native_ctx: Any,
    format_trace_fn: Callable[[str, str, dict[str, Any], Any], str],
    with_footer_fn: Callable[[str, str], str],
    append_activity_fn: Callable[[str], str],
) -> str:
    """Append optional trace block and interaction footer or activity suffix."""
    if trace and trace_ctx is not None:
        params = {
            "temperature": temperature,
            "sampling": sampling_desc,
            "num_predict": num_predict,
            "num_ctx": num_ctx,
            "num_ctx_native": native_ctx,
        }
        traced_response = response + format_trace_fn(model, tier_label, params, trace_ctx)
        return (
            with_footer_fn(traced_response, footer_iid)
            if footer_iid is not None
            else append_activity_fn(traced_response)
        )
    if footer_iid is not None:
        return with_footer_fn(response, footer_iid)
    return append_activity_fn(response)


_RETRYABLE_MASTER_STATUSES = frozenset(
    {"interrupted", "failed", "task_drift", "cancelled"}
)
_VALID_RETRY_MODES = frozenset(
    {
        "inline",
        "master",
        "delegate",
        "delegated",
        "agents",
        "parallel",
        "fleet",
        "swarm",
        "fanout",
    }
)


def validate_and_plan_master_retry(
    candidate: Mapping[str, Any] | None,
    *,
    selector: str,
    tier: str,
    parse_objectives: Callable[[str], Any],
    provenance_error_type: type[Exception],
    task_digest: Callable[[str], str],
    is_dir: Callable[[str], bool],
) -> tuple[dict[str, Any], str, str, int, str, str]:
    """Validate persisted master provenance and build retry orchestration kwargs.

    Returns ``(retry_kwargs, status, mode, agents, retry_tier, error_detail)``.
    """
    if candidate is None:
        return (
            {},
            "",
            "",
            0,
            "",
            "no unambiguous persisted master matched %r." % selector,
        )
    status = str(candidate.get("status") or "")
    if status not in _RETRYABLE_MASTER_STATUSES:
        return (
            {},
            status,
            "",
            0,
            "",
            "master %s is %s; only interrupted/failed/cancelled work or task_drift work can be retried."
            % (candidate["id"], status or "unknown"),
        )
    task = str(candidate.get("task") or "").strip()
    if not task:
        return (
            {},
            status,
            "",
            0,
            "",
            "persisted master %s has no recoverable task text." % candidate["id"],
        )
    try:
        objectives = parse_objectives(task)
    except provenance_error_type as exc:
        return (
            {},
            status,
            "",
            0,
            "",
            "persisted master provenance is invalid: %s" % exc,
        )
    stored_digest = str(candidate.get("master_task_digest") or "")
    recovered_digest = task_digest(task)
    stored_objective_ids = list(candidate.get("objective_ids") or ())
    recovered_objective_ids = [obj.objective_id for obj in objectives]
    if stored_digest and stored_digest != recovered_digest:
        return (
            {},
            status,
            "",
            0,
            "",
            "persisted master task text no longer matches its immutable digest; "
            "start a fresh orchestration instead of retrying changed work.",
        )
    if stored_objective_ids != recovered_objective_ids:
        return (
            {},
            status,
            "",
            0,
            "",
            "persisted master objective IDs no longer match the task contract; "
            "start a fresh orchestration instead of retrying ambiguous work.",
        )
    if objectives and not stored_digest:
        return (
            {},
            status,
            "",
            0,
            "",
            "protected legacy master has no immutable task digest; start a "
            "fresh orchestration instead of retrying unverifiable work.",
        )
    mode = str(candidate.get("mode") or "delegated").lower()
    if mode not in _VALID_RETRY_MODES:
        mode = "delegated"
    agents = int(candidate.get("requested_agents") or 3)
    retry_tier = str(tier or "code").strip() or "code"
    retry_kwargs: dict[str, Any] = {
        "task": task,
        "mode": mode,
        "agents": agents,
        "tier": retry_tier,
        "learn": False,
        "retry_of": candidate["id"],
    }
    retry_project = str(candidate.get("project") or "").strip()
    if not retry_project:
        for persisted_path in candidate.get("files") or ():
            value = str(persisted_path or "").strip()
            if value and is_dir(value):
                retry_project = value
                break
    if retry_project:
        retry_kwargs["project"] = retry_project
    return retry_kwargs, status, mode, agents, retry_tier, ""


def format_master_retry_report(
    candidate: Mapping[str, Any],
    *,
    status: str,
    mode: str,
    agents: int,
    retry_tier: str,
    result: str,
) -> str:
    """Format the response wrapper for a persisted master retry."""
    return "\n".join(
        [
            "persisted master retry",
            "  source: %s [%s] | mode: %s | agents: %d"
            % (candidate["id"], status, mode, agents),
            "  tier: %s (explicit/local-safe default; original=%s)"
            % (retry_tier, candidate.get("tier") or "unknown"),
            "",
            result,
        ]
    ).strip()
