"""Structured state machine and sub-phase helpers for ``_agent_turn``."""
from __future__ import annotations

import collections
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sonder_runtime.application.server_core.agent_prompts import (
    AGENT_DEDUPLICATED_INSPECTION_TOOLS,
    AGENT_EXECUTION_STATE_INVALIDATION_TOOLS,
    AGENT_FILE_EVIDENCE_TOOLS,
    AGENT_WEB_GATED_TOOLS,
    PROJECT_BOUND_AGENT_TOOLS,
    PROJECT_SCOPED_COMMAND_TOOLS,
    WORK_INSPECTION_TOOLS,
    WORK_VALIDATION_TOOLS,
    attach_tool_evidence,
    build_failure_recovery_hint,
)


@dataclass
class AgentTurnState:
    """Mutable per-turn ledger owned by ``_agent_turn``."""

    observations: list[str] = field(default_factory=list)
    file_evidence: bool = False
    used_tool: bool = False
    inspected: bool = False
    mutated: bool = False
    parent_effect_dirty: bool = False
    validation_attempted: bool = False
    validation_ok: bool = False
    verification_ok: bool = False
    mutations: list[Any] = field(default_factory=list)
    used_tool_names: set[str] = field(default_factory=set)
    successful_web_calls: set[str] = field(default_factory=set)
    successful_inspection_results: dict[str, str] = field(default_factory=dict)
    repeated_inspection_counts: dict[str, int] = field(default_factory=dict)
    failed_call_counts: dict[str, int] = field(default_factory=dict)
    semantic_no_progress: collections.deque[tuple[str, str, str]] = field(
        default_factory=lambda: collections.deque(maxlen=6)
    )
    completion_blocking_failures: dict[str, tuple[str, str]] = field(default_factory=dict)
    run_created_paths: set[Any] = field(default_factory=set)
    claim_review_requests: int = 0
    claim_review_policy_refused: bool = False
    claim_review_verified: bool = False
    pre_model_context_rendered: bool = False
    pre_model_context_response_observed: bool = False
    model_failure: dict[str, Any] = field(default_factory=dict)
    last_tool_name: str | None = None
    delegated_verdict: Any = None


@dataclass(frozen=True)
class FinalGateResult:
    """Verdict when the model proposes ``{"final": ...}`` during a step."""

    action: str  # "proceed" | "continue" | "early_exit" | "finish"
    observation: str = ""
    exit_message: str = ""
    mark_mutation_done: bool = False
    mark_validation_in_progress: bool = False


@dataclass(frozen=True)
class ClaimReviewStepResult:
    """Outcome of handling one negative-claim review decision."""

    action: str  # "accept" | "continue" | "early_exit"
    exit_message: str = ""
    checklist_failure_reason: str = ""


@dataclass(frozen=True)
class PreDispatchResult:
    """Result of pre-dispatch policy, auto-promote, dedup, and batch checks."""

    policy_tool_args: dict[str, Any]
    auto_promoted_overwrite: bool
    call_signature: str
    cached_inspection: bool
    prior_identical_failures: int
    policy_error: str
    batch_refusal: Any
    blocked_observation: str | None


@dataclass(frozen=True)
class StepOutcomeResult:
    """Result of post-dispatch state accounting for one tool step."""

    observation_text: str
    model_observation: str
    mutation_attempt_may_have_changed: bool
    execution_may_have_changed: bool
    early_exit_message: str | None = None
    checklist_failure: tuple[str, int] | None = None


@dataclass(frozen=True)
class FinalReportAssembly:
    """Computed text, status, checklist actions, and receipt metadata for ``finish_final``."""

    final_text: str
    failed: bool
    validated: bool
    deferred: bool
    validation_failed: bool
    model_summary: str
    result_summary: str
    response_status: tuple[str, str] | None
    certificate_fields: dict[str, Any]
    final_class: str
    note_vacuous_completion: bool
    checklist_marks: tuple[tuple[int, str, str], ...]


def classify_terminal_marker(
    text: str,
    prefixes: Sequence[str],
    default: str,
) -> str:
    """Return the stripped failure/standing marker prefixing ``text``, or ``default``."""
    stripped = text.lstrip()
    for marker in prefixes:
        if stripped.startswith(marker):
            return marker.rstrip(":")
    return default


def format_blocking_failures_final(
    completion_blocking_failures: Mapping[str, tuple[str, str]],
) -> str:
    """Format the terminal message when required host evidence failed and did not recover."""
    failures = "; ".join(
        "%s: %s" % (name, detail[:240])
        for _signature, (name, detail) in sorted(completion_blocking_failures.items())
    )
    evidence_failure = any(
        name in AGENT_FILE_EVIDENCE_TOOLS
        for name, _detail in completion_blocking_failures.values()
    )
    prefix = "EVIDENCE_REQUIRED" if evidence_failure else "ERROR"
    return "%s: required host evidence did not recover (%s)." % (prefix, failures)


def evaluate_final_decision_gate(
    state: AgentTurnState,
    *,
    step: int,
    max_steps: int,
    required_tools: frozenset[str],
    auto_checklist: bool,
    work_validated: Callable[[], bool],
    artifact_deferred: Callable[[], bool],
    artifact_guidance: Callable[[], str],
    error_prefix: str,
) -> FinalGateResult:
    """Evaluate host gates before accepting a step-level ``{"final": ...}`` decision."""
    if state.completion_blocking_failures:
        if step < max_steps:
            return FinalGateResult(
                action="continue",
                observation=(
                    "HOST REQUIREMENT: retry and successfully complete the failed "
                    "evidence tool(s): %s. An unrelated successful tool does not "
                    "recover this failure."
                    % ", ".join(sorted({
                        name for name, _detail in state.completion_blocking_failures.values()
                    }))
                ),
            )
        return FinalGateResult(action="finish")
    if required_tools and not (required_tools & state.used_tool_names):
        if step < max_steps:
            return FinalGateResult(
                action="continue",
                observation=(
                    "HOST REQUIREMENT: use at least one successful tool from: %s."
                    % ", ".join(sorted(required_tools))
                ),
            )
        return FinalGateResult(
            action="early_exit",
            exit_message=(
                "%s agent reached max_steps=%d without using a required "
                "web tool (%s)."
                % (error_prefix, max_steps, ", ".join(sorted(required_tools)))
            ),
        )
    if auto_checklist and not state.used_tool and step < max_steps:
        return FinalGateResult(
            action="continue",
            observation=(
                "HOST REQUIREMENT: use at least one relevant inspection or execution tool before final."
            ),
        )
    if (
        auto_checklist
        and state.mutated
        and not work_validated()
        and not artifact_deferred()
        and step < max_steps
    ):
        return FinalGateResult(
            action="continue",
            observation=(
                "HOST REQUIREMENT: files changed but no grounded validation has passed. "
                + artifact_guidance()
            ),
            mark_mutation_done=True,
            mark_validation_in_progress=True,
        )
    return FinalGateResult(action="proceed")


def apply_claim_review_decision(
    state: AgentTurnState,
    claim_review: Mapping[str, Any],
    *,
    run_claim_review_action: Callable[[Mapping[str, Any], int], str],
    evidence_required_marker: str,
    is_finalization: bool,
) -> ClaimReviewStepResult:
    """Process one negative-claim reviewer verdict against ``state``."""
    decision = str(claim_review.get("decision") or "")
    reason = str(claim_review.get("reason") or "")
    if decision == "error":
        return ClaimReviewStepResult(
            action="early_exit",
            exit_message=reason,
            checklist_failure_reason=(
                "negative-claim reviewer model request failed at finalization"
                if is_finalization
                else "negative-claim reviewer model request failed"
            ),
        )
    if decision == "continue":
        state.claim_review_requests += 1
        if state.claim_review_requests <= 2:
            state.observations.append(
                "HOST CLAIM REVIEW: %s\n%s"
                % (
                    reason,
                    run_claim_review_action(claim_review, state.claim_review_requests),
                )
            )
            return ClaimReviewStepResult(action="continue")
        return ClaimReviewStepResult(
            action="early_exit",
            exit_message="%s: %s\n\n%s" % (
                evidence_required_marker,
                reason,
                "\n\n".join(state.observations),
            ),
            checklist_failure_reason=(
                "negative existence claim lacked exact evidence at finalization"
                if is_finalization
                else "negative existence claim lacked exact evidence"
            ),
        )
    if state.claim_review_policy_refused and not state.claim_review_verified:
        return ClaimReviewStepResult(
            action="early_exit",
            exit_message="%s: %s\n\n%s" % (
                evidence_required_marker,
                "the negative existence claim was never verified -- host policy "
                "refused every claim-review evidence tool this run offered, so "
                "the reviewer's acceptance carries no evidence",
                "\n\n".join(state.observations),
            ),
            checklist_failure_reason=(
                "negative-claim review could not run: host policy refused "
                "every evidence tool it was given"
            ),
        )
    return ClaimReviewStepResult(action="accept")


def evaluate_pre_dispatch(
    state: AgentTurnState,
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    policy_tool_args: dict[str, Any],
    allowed_tools: frozenset[str] | None,
    tool_policy: Callable[[str, dict[str, Any]], Any] | None,
    cloud: bool,
    unsafe: bool,
    project_scope: str,
    project_bound: bool,
    read_only: bool,
    auto_checklist: bool,
    created_path_key: Callable[[Any], Any],
    call_signature_fn: Callable[[str, dict[str, Any]], str],
    cloud_policy_error_fn: Callable[..., str],
    scope_path_error_fn: Callable[[str, dict[str, Any], str], str],
    execution_arg_error_fn: Callable[[str, dict[str, Any], str], str],
    read_only_error_fn: Callable[..., str],
    tool_mutates_fn: Callable[[str, dict[str, Any]], bool],
    batch_guard: Any,
    batch_telemetry_fn: Callable[[Any], None],
    error_prefix: str,
) -> PreDispatchResult:
    """Run auto-promote, policy gates, failure/web dedup, and batch checks."""
    auto_promoted_overwrite = False
    if (
        tool_name == "file_write"
        and str(policy_tool_args.get("mode") or "create").lower() == "create"
        and created_path_key(policy_tool_args.get("path")) in state.run_created_paths
    ):
        policy_tool_args = dict(policy_tool_args)
        policy_tool_args["mode"] = "overwrite"
        auto_promoted_overwrite = True

    call_signature = call_signature_fn(tool_name, policy_tool_args)
    cached_inspection = (
        tool_name in AGENT_DEDUPLICATED_INSPECTION_TOOLS
        and call_signature in state.successful_inspection_results
    )
    prior_identical_failures = state.failed_call_counts.get(call_signature, 0)

    policy_error = ""
    if allowed_tools is not None and tool_name not in allowed_tools:
        policy_error = (
            "%s HOST POLICY: tool '%s' is outside this autonomous run's allowlist."
            % (error_prefix, tool_name)
        )
    if not policy_error and tool_policy is not None:
        policy_error = str(tool_policy(tool_name, tool_args) or "")
    if not policy_error and cloud:
        policy_error = cloud_policy_error_fn(tool_name, unsafe=unsafe)
    if not policy_error and project_scope:
        policy_error = scope_path_error_fn(tool_name, policy_tool_args, project_scope)
    if not policy_error and project_bound and tool_name not in PROJECT_BOUND_AGENT_TOOLS:
        policy_error = (
            "%s HOST POLICY: tool '%s' has no project-bound execution "
            "contract and is disabled for a project-bound agent."
            % (error_prefix, tool_name)
        )
    if not policy_error and (project_scope or tool_name in PROJECT_SCOPED_COMMAND_TOOLS):
        policy_error = execution_arg_error_fn(tool_name, policy_tool_args, project_scope)
    if not policy_error and read_only:
        policy_error = read_only_error_fn(
            tool_name, policy_tool_args, trusted_extra_roots=project_scope,
        )
    if (
        auto_checklist
        and tool_mutates_fn(tool_name, policy_tool_args)
        and not state.inspected
        and not policy_error
    ):
        policy_error = (
            "%s HOST REQUIREMENT: inspect relevant workspace evidence "
            "before making a mutation." % error_prefix
        )

    batch_refusal: Any = None
    if (
        prior_identical_failures < 2
        and not policy_error
        and not cached_inspection
        and call_signature not in state.completion_blocking_failures
    ):
        batch_refusal = batch_guard.before_dispatch(tool_name, policy_tool_args)
        if batch_refusal is not None:
            batch_telemetry_fn(batch_refusal.telemetry())

    blocked_observation: str | None = None
    if prior_identical_failures >= 2:
        blocked_observation = (
            "%s HOST NO-PROGRESS: this exact tool call already failed twice. "
            "It was not run again. Change its arguments, inspect/discover the "
            "correct target, or choose a different recovery tool." % error_prefix
        )
    elif (
        tool_name in AGENT_WEB_GATED_TOOLS
        and call_signature in state.successful_web_calls
    ):
        blocked_observation = (
            "%s HOST REQUIREMENT: this identical web tool call already "
            "succeeded; use its existing observation or choose a different call."
            % error_prefix
        )
    elif policy_error:
        blocked_observation = policy_error
    elif batch_refusal is not None:
        blocked_observation = str(batch_refusal.render())

    return PreDispatchResult(
        policy_tool_args=policy_tool_args,
        auto_promoted_overwrite=auto_promoted_overwrite,
        call_signature=call_signature,
        cached_inspection=cached_inspection,
        prior_identical_failures=prior_identical_failures,
        policy_error=policy_error,
        batch_refusal=batch_refusal,
        blocked_observation=blocked_observation,
    )


def record_step_outcome(
    state: AgentTurnState,
    *,
    step: int,
    tool_name: str,
    tool_args: dict[str, Any],
    policy_tool_args: dict[str, Any],
    call_signature: str,
    reason: Any,
    observation: Any,
    normalization_notes: Sequence[str],
    tool_ok: bool,
    tool_dispatched: bool,
    cached_inspection: bool,
    auto_promoted_overwrite: bool,
    prior_identical_failures: int,
    batch_refusal: Any,
    read_only: bool,
    require_file_evidence: bool,
    auto_checklist: bool,
    project_scope: str,
    required_tools: frozenset[str],
    abort_on_tool_failure: frozenset[str],
    verification_tools: frozenset[str],
    created_path_key: Callable[[Any], Any],
    batch_operations: Callable[[Mapping[str, Any]], list[Any] | None],
    tool_mutates_fn: Callable[[str, dict[str, Any]], bool],
    mutation_records_fn: Callable[[str, dict[str, Any]], Sequence[Any]],
    verification_covers_fn: Callable[..., bool],
    validation_covers_fn: Callable[[str, dict[str, Any], list[Any], str], bool],
    model_observation_view_fn: Callable[[str, str], str],
    mark_checklist_fn: Callable[[int, str, str], None],
    fail_checklist_fn: Callable[[str, int], None],
    spec_prefetcher: Any,
    artifact_gate: Any,
    batch_guard: Any,
    batch_telemetry_fn: Callable[[Any], None],
    host_controller: Any,
    ledger_safe_args_fn: Callable[[dict[str, Any]], Any],
    peer_updates: str,
    error_prefix: str,
) -> StepOutcomeResult:
    """Apply all host state updates, cache invalidation, and stall checks for one step."""
    norm_suffix = (
        "\nargument normalization: "
        + "; ".join(normalization_notes).replace("\n", " ").replace("\r", " ")[:600]
        if normalization_notes
        else ""
    )
    observation_text = str(observation) + norm_suffix
    abort_observation: str | None = None
    checklist_failure: tuple[str, int] | None = None

    if tool_ok:
        state.failed_call_counts.pop(call_signature, None)
        state.completion_blocking_failures.pop(call_signature, None)
        if tool_name == "file_write":
            state.run_created_paths.add(created_path_key(policy_tool_args.get("path")))
            if auto_promoted_overwrite:
                observation_text += (
                    "\nHOST AUTO-PROMOTE: mode=create was replaced with "
                    "mode=overwrite because this run created the file."
                )
        elif tool_name == "file_batch_write":
            for item in batch_operations(policy_tool_args) or []:
                if (
                    isinstance(item, dict)
                    and str(item.get("mode") or "").lower() == "create"
                ):
                    state.run_created_paths.add(created_path_key(item.get("path")))
        elif tool_name == "data_convert" and policy_tool_args.get("apply") is True:
            state.run_created_paths.add(
                created_path_key(policy_tool_args.get("output_path"))
            )
    else:
        state.failed_call_counts[call_signature] = prior_identical_failures + 1
        if tool_dispatched and tool_name in abort_on_tool_failure:
            if auto_checklist:
                fail_checklist_fn(
                    "%s failed: %s" % (tool_name, observation_text[:240]), 1,
                )
            abort_observation = observation_text[:600]
        if tool_dispatched and (
            (len(required_tools) == 1 and tool_name in required_tools)
            or (
                read_only
                and require_file_evidence
                and tool_name in AGENT_FILE_EVIDENCE_TOOLS
            )
        ):
            state.completion_blocking_failures[call_signature] = (
                tool_name, observation_text[:600],
            )
        recovery = build_failure_recovery_hint(
            tool_name,
            observation_text,
            blocks_completion=call_signature in state.completion_blocking_failures,
        )
        observation_text += "\n" + recovery

    state.used_tool = state.used_tool or tool_ok
    spec_prefetcher.observe(tool_name, tool_args, observation_text, ok=tool_ok)

    if tool_ok:
        state.used_tool_names.add(str(tool_name))
        if tool_name in AGENT_WEB_GATED_TOOLS:
            state.successful_web_calls.add(call_signature)
        if tool_name in AGENT_DEDUPLICATED_INSPECTION_TOOLS and not cached_inspection:
            state.successful_inspection_results[call_signature] = (
                model_observation_view_fn(tool_name, observation_text)
            )
            state.repeated_inspection_counts.pop(call_signature, None)
    if tool_name in AGENT_FILE_EVIDENCE_TOOLS and tool_ok:
        state.file_evidence = True
    if auto_checklist and tool_name in WORK_INSPECTION_TOOLS and tool_ok:
        state.inspected = True
        mark_checklist_fn(1, "done", "%s completed" % tool_name)
        mark_checklist_fn(2, "in_progress", "working from inspected evidence")

    mutates = tool_mutates_fn(tool_name, policy_tool_args) and tool_name != "agent_lane"
    mutation_happened = mutates and tool_ok
    mutation_attempt_may_have_changed = tool_dispatched and mutates
    if mutation_attempt_may_have_changed:
        state.mutated = True
    execution_may_have_changed = (
        tool_dispatched and tool_name in AGENT_EXECUTION_STATE_INVALIDATION_TOOLS
    )
    if tool_dispatched:
        artifact_gate.observe(
            tool_name,
            policy_tool_args,
            observation_text,
            success=tool_ok,
            mutation=mutation_attempt_may_have_changed,
            execution=execution_may_have_changed,
        )
    batch_advisory: Any = None
    if tool_dispatched and not (
        mutation_attempt_may_have_changed or execution_may_have_changed
    ):
        batch_advisory = batch_guard.after_dispatch(
            tool_name, policy_tool_args, ok=tool_ok,
        )
        if batch_advisory is not None:
            batch_telemetry_fn(batch_advisory.telemetry())
    if mutation_attempt_may_have_changed or execution_may_have_changed:
        state.parent_effect_dirty = True
        batch_guard.reset()
        state.successful_inspection_results.clear()
        state.repeated_inspection_counts.clear()
        state.validation_attempted = False
        state.validation_ok = False
        state.verification_ok = False
        if mutation_happened or tool_ok:
            state.failed_call_counts.clear()
        else:
            current_failure_count = state.failed_call_counts.get(call_signature, 0)
            state.failed_call_counts.clear()
            if current_failure_count:
                state.failed_call_counts[call_signature] = current_failure_count
    if mutation_attempt_may_have_changed:
        for record in mutation_records_fn(tool_name, policy_tool_args):
            if record not in state.mutations:
                state.mutations.append(record)
        if auto_checklist and state.mutated:
            mark_checklist_fn(1, "done", "inspection completed before mutation")
            mark_checklist_fn(2, "in_progress", "%s changed workspace state" % tool_name)
    if tool_name in verification_tools:
        state.validation_attempted = True
        state.verification_ok = tool_ok and verification_covers_fn(
            tool_name, policy_tool_args, state.mutations, project_scope=project_scope,
        )
    validation_covered = False
    if tool_name in WORK_VALIDATION_TOOLS:
        state.validation_attempted = True
        validation_covered = tool_ok and validation_covers_fn(
            tool_name, policy_tool_args, state.mutations, observation_text,
        )
        state.validation_ok = validation_covered
        if state.mutated and tool_ok and not validation_covered:
            observation_text += (
                "\nHOST VALIDATION: this check did not cover the changed on-disk path(s). "
                "Run the edited script, a workspace test/build, or a path-specific verifier."
            )
        if auto_checklist and state.mutated:
            mark_checklist_fn(2, "done", "implementation phase complete")
            mark_checklist_fn(
                3,
                "done" if validation_covered else "blocked",
                "%s %s" % (
                    tool_name,
                    "passed and covered changed paths"
                    if validation_covered
                    else "did not validate changed paths",
                ),
            )

    semantic_stall: tuple[str, str, int] | None = None
    evidence_progress = (
        tool_dispatched
        and tool_ok
        and tool_name in AGENT_FILE_EVIDENCE_TOOLS
        and bool(str(observation).strip())
    )
    if mutation_happened or evidence_progress or (
        tool_name in WORK_VALIDATION_TOOLS and validation_covered
    ):
        state.semantic_no_progress.clear()
    else:
        outcome_class = (
            "failed"
            if not tool_ok
            else "empty"
            if not str(observation).strip()
            else ""
        )
        if tool_dispatched and outcome_class:
            state.semantic_no_progress.append((tool_name, outcome_class, call_signature))
            matching = [
                item
                for item in state.semantic_no_progress
                if item[0] == tool_name and item[1] == outcome_class
            ]
            distinct_signatures = {item[2] for item in matching}
            if len(matching) >= 4 and len(distinct_signatures) >= 3:
                semantic_stall = (tool_name, outcome_class, len(distinct_signatures))

    if host_controller is not None:
        host_controller.observe_host_tool(
            tool=tool_name,
            arguments=ledger_safe_args_fn(policy_tool_args),
            observation=observation_text,
            dispatched=bool(tool_dispatched),
            success=bool(tool_ok),
            dirty=bool(mutation_attempt_may_have_changed or execution_may_have_changed),
            mutation_records=(
                mutation_records_fn(tool_name, policy_tool_args)
                if mutation_attempt_may_have_changed
                else ()
            ),
            verifier=tool_name in verification_tools,
            validator=tool_name in WORK_VALIDATION_TOOLS,
        )

    model_observation = model_observation_view_fn(tool_name, observation_text)
    if batch_advisory is not None:
        model_observation += "\n" + str(batch_advisory.render())
    state.observations.append(
        "step %d tool=%s reason=%s\n%s" % (step, tool_name, reason, model_observation)
    )
    if peer_updates:
        state.observations.append(peer_updates)

    early_exit_message: str | None = None
    if batch_refusal is not None and batch_refusal.exhausted:
        checklist_failure = ("model ignored repeated batch-tool steering", 2)
        early_exit_message = (
            "%s agent kept issuing single %s calls after %d batch-guard "
            "refusals; use %s for multi-target reads.\n\n%s"
            % (
                error_prefix,
                batch_refusal.counterpart.single_tool,
                batch_refusal.refusals,
                batch_refusal.counterpart.batch_tool,
                "\n\n".join(state.observations),
            )
        )
    elif semantic_stall is not None:
        stalled_tool, stall_class, distinct_count = semantic_stall
        early_exit_message = (
            "%s agent made no semantic progress: %s produced %s outcomes "
            "across %d distinct calls. Change the recovery strategy or make "
            "a state-changing call."
            % (error_prefix, stalled_tool, stall_class, distinct_count)
        )
    elif abort_observation is not None:
        early_exit_message = (
            "%s required %s failed; no answer was produced from "
            "unverified sources (%s)." % (error_prefix, tool_name, abort_observation)
        )

    return StepOutcomeResult(
        observation_text=observation_text,
        model_observation=model_observation,
        mutation_attempt_may_have_changed=mutation_attempt_may_have_changed,
        execution_may_have_changed=execution_may_have_changed,
        early_exit_message=early_exit_message,
        checklist_failure=checklist_failure,
    )


def assemble_final_report(
    *,
    final: str,
    failed: bool,
    state: AgentTurnState,
    delegated: bool,
    validated: bool,
    artifact_assessment: Mapping[str, Any],
    auto_checklist: bool,
    read_only: bool,
    allowed_tools: frozenset[str] | None,
    verifier_reachable_fn: Callable[[bool, frozenset[str] | None], bool],
    verification_standing_fn: Callable[[], tuple[bool, str]],
    unverified_banner_fn: Callable[[str, str], str],
    report_delegated_outcome_fn: Callable[[str], str],
    failure_prefixes: Sequence[str],
    validation_failed_line: str,
    verifiers_phrase: str,
    unverified_prefix: str,
) -> FinalReportAssembly:
    """Assemble final banners, checklist marks, summary, and classification."""
    deferred = not (failed or delegated or validated) and bool(artifact_assessment["deferred"])
    state.validation_attempted |= bool(artifact_assessment["attempted"])
    if delegated and state.delegated_verdict is not None:
        state.validation_attempted = True

    checklist_marks: list[tuple[int, str, str]] = []
    if auto_checklist:
        validation_status = (
            "done"
            if (not failed and (validated or not (state.mutated or delegated)))
            else "blocked"
        )
        checklist_marks = [
            (1, "done", "workspace evidence inspected"),
            (
                2,
                "blocked" if failed else "done",
                "requested work completed"
                if state.mutated
                else "analysis completed without file mutation",
            ),
            (
                3,
                validation_status,
                "grounded validation passed"
                if validated
                else (
                    "no mutation required"
                    if not state.mutated
                    else "needs approval for execution"
                    if deferred
                    else "validation did not pass"
                ),
            ),
            (4, "done", "end report prepared"),
        ]

    model_summary = final.splitlines()[0] if final else "agent completed"
    validation_failed = bool(
        auto_checklist and (state.mutated or delegated) and not (validated or deferred)
    )
    standing = ""
    if (
        not deferred
        and not validated
        and not state.verification_ok
        and verifier_reachable_fn(read_only, allowed_tools)
    ):
        demanded, reason = verification_standing_fn()
        if demanded:
            standing = reason

    note_vacuous_completion = False
    if validation_failed and not failed:
        block = validation_failed_line
        if standing:
            block += "\nThis run also claimed completion without %s - %s" % (
                verifiers_phrase, standing,
            )
        final = block + "\n\n" + final
    elif standing and not failed:
        if state.mutated or delegated:
            final = unverified_banner_fn(final, standing)
        if not state.mutated and not state.validation_attempted:
            note_vacuous_completion = True

    result_summary = validation_failed_line if validation_failed else model_summary
    response_status: tuple[str, str] | None = None
    if deferred:
        final = "written, not executed: needs %s to verify\n\n%s" % (
            artifact_assessment["required"], final,
        )
        response_status = ("unverified", "written; execution needs approval")
    elif validated and artifact_assessment["passed"]:
        final = "Static checks passed; artifact not executed.\n\n" + final

    certificate_fields: dict[str, Any] = {}
    if delegated:
        if state.delegated_verdict is not None:
            certificate_fields = {
                "verification_certificate_id": state.delegated_verdict.certificate_id,
                "verification_generation": state.delegated_verdict.generation,
                "verification_code": state.delegated_verdict.code,
            }
        if validated:
            final = (
                "Delegated workspace certificate valid for generation %s.\n\n"
                "Model outcome:\n%s" % (state.delegated_verdict.generation, final)
            )
        else:
            final = report_delegated_outcome_fn(final)
            response_status = (
                "unverified",
                "delegated work or parent effects lack current validation",
            )
            result_summary = "delegated work or parent effects lack current validation"

    final_class = classify_terminal_marker(
        final,
        (*failure_prefixes, unverified_prefix),
        "ERROR" if failed else "UNVERIFIED" if deferred else "NORMAL",
    )
    return FinalReportAssembly(
        final_text=final,
        failed=failed,
        validated=validated,
        deferred=deferred,
        validation_failed=validation_failed,
        model_summary=model_summary,
        result_summary=result_summary,
        response_status=response_status,
        certificate_fields=certificate_fields,
        final_class=final_class,
        note_vacuous_completion=note_vacuous_completion,
        checklist_marks=tuple(checklist_marks),
    )


def prepare_evidence_attached_final(
    final: str,
    *,
    failed: bool,
    state: AgentTurnState,
    failure_prefixes: Sequence[str],
    include_evidence: bool,
) -> tuple[str, bool]:
    """Normalize leading failure markers, apply blocking failures, and attach evidence."""
    if final.lstrip().startswith(tuple(failure_prefixes)):
        final = final.lstrip()
        failed = True
    if state.completion_blocking_failures:
        final = format_blocking_failures_final(state.completion_blocking_failures)
        failed = True
    return final, failed


def attach_turn_evidence(
    text: str,
    observations: Sequence[str],
    *,
    include_evidence: bool,
) -> str:
    """Attach host-owned tool evidence block when enabled."""
    return attach_tool_evidence(text, observations, include_evidence=include_evidence)
