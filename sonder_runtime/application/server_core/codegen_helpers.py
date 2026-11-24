"""Structured planning, canary budgeting, and per-file repair loop for ``codegen_build_loop``."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sonder_runtime.application.context_planner import (
    CONTEXT_SECTIONS,
    ContextPlanner,
    ModelContext,
)
from sonder_runtime.application.memory.strategy_memory import language_from_path
from sonder_runtime.application.strategy.codegen_result import (
    CodegenCanaryStop,
    render_codegen_canary_stop,
)
from sonder_runtime.domain.strategy.models import (
    FailureClass,
    StrategyAction,
    StrategyBudget,
    StrategyUsage,
)
from sonder_runtime.domain.verification_progress import (
    MAX_VERIFICATION_ATTEMPTS,
    VerificationProgressGuard,
    outcome_fingerprint,
)

_LOGGER = logging.getLogger(__name__)

_ACTIVE_CANARY_ACTIONS: frozenset[StrategyAction] = frozenset({
    StrategyAction.REPAIR,
    StrategyAction.CRITIC,
    StrategyAction.SWITCH_MODEL,
})


@dataclass(frozen=True)
class CodegenOperationPlan:
    """Resolved operation metadata for one ``codegen_build_loop`` invocation."""

    project_identity: str
    scope_run_id: str
    operation_id: str
    member_run_ids: tuple[str, ...]
    objective_digest: str
    repair_tiers: tuple[str, str] | tuple[()]
    repair_models: tuple[str, str] | tuple[()]
    canary_route: bool
    active_operation: bool
    attempt_wall_ceiling: int


@dataclass(frozen=True)
class CodegenFileBudget:
    """Per-file context windows and sealed canary budget."""

    generation_context_window: int = 0
    critic_context_window: int = 0
    alternate_context_window: int = 0
    generation_token_ceiling: int = 0
    critic_token_ceiling: int = 0
    alternate_token_ceiling: int = 0
    canary_budget: StrategyBudget | None = None


@dataclass
class CanaryClock:
    """Shared wall-deadline tracker for one codegen operation and active attempt."""

    operation_deadline: float | None = None
    attempt_deadline: float | None = None

    def remaining_seconds(self) -> int | None:
        deadlines = [
            value
            for value in (self.operation_deadline, self.attempt_deadline)
            if value is not None
        ]
        if not deadlines:
            return None
        remaining = min(deadlines) - time.monotonic()
        if remaining < 1.0:
            raise TimeoutError("codegen canary wall deadline exhausted")
        return max(1, int(remaining))


@dataclass(frozen=True)
class CodegenFileStepResult:
    """Outcome of running codegen attempts for one target file."""

    row: dict[str, str] | None = None
    stop_reply: str | None = None
    write_failure: tuple[str, str, Exception] | None = None


class StrategyTraceLike(Protocol):
    """Minimal protocol for strategy trace reservation."""

    def reserve_next(
        self,
        run_id: str,
        attempt_id: str,
        *,
        objective_digest: str,
        action: StrategyAction,
        usage: StrategyUsage,
        budget: StrategyBudget | None,
    ) -> Any: ...


class StrategyMemoryLike(Protocol):
    """Minimal protocol for recovery context queries."""

    def recovery_context(
        self,
        run_id: str,
        attempt_id: str,
        *,
        project_scope: str,
        plan: Any,
        failure_class: FailureClass,
        family: str,
        language: str,
    ) -> Any: ...


class CodegenBuildState(Protocol):
    """Minimal mutable build-state mapping."""

    def __getitem__(self, key: str) -> bool: ...


@dataclass(frozen=True)
class CodegenAttemptHooks:
    """Callbacks injected by ``server.codegen_build_loop`` for I/O and inference."""

    read_file: Callable[[str], str]
    write_file: Callable[[str, str], Any]
    run_build: Callable[[], list[str]]
    build_state: Mapping[str, bool]
    auto_model_context: Callable[[str], int]
    is_local_ollama_route: Callable[[str, str], bool]
    local_num_ctx: Callable[[str], int]
    observe_build: Callable[..., Any]
    run_critic: Callable[
        [str, str, list[str], str, str, int, tuple[str, str], tuple[str, str]],
        tuple[bool, Any],
    ]
    generate_pinned: Callable[[str, str, int], str]
    generate_observed: Callable[[str, str, str, int], str]
    generate_ensemble: Callable[[str, str], str]
    format_model_call_error: Callable[[BaseException], str]
    is_model_call_error: Callable[[BaseException], bool]
    is_cancelled_error: Callable[[BaseException], bool]
    dependency_brief: Callable[[dict[str, str]], str]
    repair_brief: Callable[[str, list[str] | None, str], str]
    strip_code: Callable[[str], str]
    apply_slips: Callable[[str, Any], tuple[str, int]]
    shrink_rejected: Callable[[str, str], bool]
    count_unreliable: Callable[[list[str]], int]
    score: Callable[[list[str]], Any]
    describe_total: Callable[[list[str]], str]
    outcome_comparable: Callable[[list[str]], bool]
    attempt_id: Callable[[str, int], str]
    objective_digest_for: Callable[[str], str]


def format_attempt_clamp_note(
    requested_raw: Any,
    requested_coerced: int,
    attempt_limit: int,
) -> str:
    """Build the clamp note when ``attempts`` was bounded."""
    if requested_coerced == attempt_limit:
        return ""
    return (
        "NOTE: attempts clamped: requested %s, ran at most %d per file "
        "(allowed range 1..%d)."
        % (requested_raw, attempt_limit, MAX_VERIFICATION_ATTEMPTS)
    )


def build_codegen_operation_plan(
    *,
    project_dir: str,
    wanted: Sequence[tuple[str, str]],
    build_program: str,
    build_args_json: str,
    attempt_limit: int,
    num_predict: Any,
    explicit_targets: Sequence[tuple[str, str]],
    is_cloud_target: Callable[[str, str], bool],
    rollout_selected: Callable[[str], bool],
) -> CodegenOperationPlan:
    """Compute immutable operation identity, repair tiers, and canary eligibility."""
    repair_tiers: tuple[str, str] | tuple[()] = ()
    repair_models: tuple[str, str] | tuple[()] = ()
    if attempt_limit >= 3 and len(explicit_targets) >= 2:
        repair_tiers = (explicit_targets[0][0], explicit_targets[1][0])
        repair_models = (explicit_targets[0][1], explicit_targets[1][1])
    project_identity = os.path.normcase(os.path.realpath(project_dir))
    scope_run_id = "codegen-scope-" + hashlib.sha256(
        os.fsencode(project_identity),
    ).hexdigest()
    operation_id = "codegen-" + uuid.uuid4().hex
    member_run_ids = tuple(
        f"{operation_id}-file-{index}" for index in range(len(wanted))
    )
    objective_digest = hashlib.sha256(
        json.dumps(
            [project_identity, list(wanted), build_program, build_args_json],
            sort_keys=True,
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    canary_route = (
        bool(explicit_targets)
        and (bool(repair_tiers) or len(explicit_targets) == 1)
        and all(
            not is_cloud_target(tier, model)
            for tier, model in explicit_targets[:2]
        )
        and type(num_predict) is int
        and 1 <= num_predict <= 8192
    )
    active_operation = bool(rollout_selected(scope_run_id)) and canary_route
    attempt_wall_ceiling = max(1, (900 - 2 * 120) // attempt_limit)
    return CodegenOperationPlan(
        project_identity=project_identity,
        scope_run_id=scope_run_id,
        operation_id=operation_id,
        member_run_ids=member_run_ids,
        objective_digest=objective_digest,
        repair_tiers=repair_tiers,
        repair_models=repair_models,
        canary_route=canary_route,
        active_operation=active_operation,
        attempt_wall_ceiling=attempt_wall_ceiling,
    )


def build_file_canary_budget(
    *,
    active_canary: bool,
    attempt_limit: int,
    attempt_wall_ceiling: int,
    num_predict: int,
    repair_tiers: tuple[str, str] | tuple[()],
    repair_models: tuple[str, str] | tuple[()],
    explicit_targets: Sequence[tuple[str, str]],
    auto_model_context: Callable[[str], int],
) -> CodegenFileBudget:
    """Build per-file model context ceilings and strategy canary budget."""
    if not active_canary:
        return CodegenFileBudget()
    base_model = repair_models[0] if repair_models else explicit_targets[0][1]
    generation_context_window = max(1, int(auto_model_context(base_model)))
    generation_token_ceiling = generation_context_window + max(64, num_predict)
    critic_context_window = 0
    critic_token_ceiling = 0
    alternate_context_window = 0
    alternate_token_ceiling = 0
    if repair_models:
        critic_context_window = max(1, int(auto_model_context(repair_models[1])))
        critic_token_ceiling = critic_context_window + max(64, min(num_predict, 512))
        alternate_context_window = critic_context_window
        alternate_token_ceiling = alternate_context_window + max(64, num_predict)
    budget = StrategyBudget(
        attempts=attempt_limit,
        model_calls=2 * attempt_limit,
        tool_calls=2 * attempt_limit,
        verifier_calls=attempt_limit,
        tokens=attempt_limit * (
            max(generation_token_ceiling, alternate_token_ceiling)
            + critic_token_ceiling
        ),
        critic_calls=int(bool(repair_tiers)),
        strategy_switches=int(bool(repair_tiers)),
        top_tier_calls=2 * attempt_limit,
        wall_seconds=attempt_wall_ceiling * attempt_limit,
    )
    return CodegenFileBudget(
        generation_context_window=generation_context_window,
        critic_context_window=critic_context_window,
        alternate_context_window=alternate_context_window,
        generation_token_ceiling=generation_token_ceiling,
        critic_token_ceiling=critic_token_ceiling,
        alternate_token_ceiling=alternate_token_ceiling,
        canary_budget=budget,
    )


def available_canary_actions(
    number: int,
    *,
    selected: bool,
    attempt_limit: int,
    repair_tiers: tuple[str, str] | tuple[()],
    comparable: bool,
    rejected: bool = False,
    unresolved: bool = False,
    critic_used: bool = False,
) -> tuple[StrategyAction, ...] | None:
    """Compute permitted next strategy actions for one canary observation."""
    if not selected:
        return None
    if number >= attempt_limit or not comparable or rejected or unresolved:
        return (StrategyAction.INSPECT,)
    actions: list[StrategyAction] = [StrategyAction.REPAIR, StrategyAction.INSPECT]
    if repair_tiers and number == 2:
        actions.append(StrategyAction.CRITIC)
    if repair_tiers and number == 3 and critic_used:
        actions.append(StrategyAction.SWITCH_MODEL)
    return tuple(actions)


def build_reserved_usage(
    budget: CodegenFileBudget,
    *,
    critic_this_attempt: bool,
    rotated_this_attempt: bool,
    attempt_wall_ceiling: int,
) -> StrategyUsage:
    """Construct the pre-dispatch strategy usage reservation for one attempt."""
    return StrategyUsage(
        attempts=1,
        model_calls=1 + int(critic_this_attempt),
        tool_calls=2,
        verifier_calls=1,
        tokens=(
            budget.alternate_token_ceiling
            if rotated_this_attempt
            else budget.generation_token_ceiling
        ) + (budget.critic_token_ceiling if critic_this_attempt else 0),
        critic_calls=int(critic_this_attempt),
        strategy_switches=int(rotated_this_attempt),
        top_tier_calls=1 + int(critic_this_attempt),
        wall_seconds=attempt_wall_ceiling,
    )


def run_codegen_file_attempts(
    *,
    file_index: int,
    name: str,
    spec: str,
    wanted: Sequence[tuple[str, str]],
    slips: Any,
    project_dir: str,
    build_program: str,
    tiers: str,
    attempt_limit: int,
    num_predict: int,
    explicit_targets: Sequence[tuple[str, str]],
    plan: CodegenOperationPlan,
    clock: CanaryClock,
    strategy_trace: StrategyTraceLike | None,
    strategy_memory: StrategyMemoryLike | None,
    hooks: CodegenAttemptHooks,
) -> CodegenFileStepResult:
    """Generate, compile, and iteratively repair one file in ``codegen_build_loop``."""
    active_canary = plan.active_operation
    strategy_run_id = (
        plan.member_run_ids[file_index]
        if active_canary
        else "codegen-" + uuid.uuid4().hex
    )
    budget = build_file_canary_budget(
        active_canary=active_canary,
        attempt_limit=attempt_limit,
        attempt_wall_ceiling=plan.attempt_wall_ceiling,
        num_predict=num_predict,
        repair_tiers=plan.repair_tiers,
        repair_models=plan.repair_models,
        explicit_targets=explicit_targets,
        auto_model_context=hooks.auto_model_context,
    )
    canary_observation_failed = False

    def observe_candidate(
        attempt_number: int,
        candidate: str,
        before: list[str],
        after: list[str],
        *,
        before_complete: bool,
        after_complete: bool,
        route: str,
        critic_used: bool = False,
        rotated: bool = False,
        rejected: bool = False,
        unresolved_effects: bool = False,
        no_progress: bool = False,
        reserved_usage: StrategyUsage | None = None,
        reserved_action: StrategyAction | None = None,
    ) -> Any:
        nonlocal canary_observation_failed
        if strategy_trace is None:
            canary_observation_failed = active_canary
            return None
        try:
            return hooks.observe_build(
                strategy_trace,
                run_id=strategy_run_id,
                file_name=name,
                project_dir=project_dir,
                spec=spec,
                build_program=build_program,
                attempt_number=attempt_number,
                attempt_limit=attempt_limit,
                code=candidate,
                before_errors=before,
                after_errors=after,
                before_complete=before_complete,
                after_complete=after_complete,
                build_ran=hooks.build_state["ran"],
                exit_ok=hooks.build_state["exit_ok"],
                route=route,
                critic_used=critic_used,
                rotated=rotated,
                rejected=rejected,
                unresolved_effects=unresolved_effects,
                memory_service=strategy_memory,
                no_progress=no_progress,
                available_actions=available_canary_actions(
                    attempt_number,
                    selected=active_canary,
                    attempt_limit=attempt_limit,
                    repair_tiers=plan.repair_tiers,
                    comparable=after_complete,
                    rejected=rejected,
                    unresolved=unresolved_effects,
                    critic_used=critic_used,
                ),
                reserved_usage=reserved_usage,
                reserved_budget=budget.canary_budget,
                reserved_action=reserved_action,
            )
        except Exception as error:  # noqa: BLE001 - optional observer cannot change the build
            canary_observation_failed = active_canary
            _LOGGER.warning(
                "codegen strategy observation failed: %s", type(error).__name__,
            )
            return None

    existing = hooks.read_file(name)
    try:
        errors = hooks.run_build()
    except TimeoutError:
        if not active_canary:
            raise
        return CodegenFileStepResult(
            stop_reply=render_codegen_canary_stop(CodegenCanaryStop.BUILD_UNCERTAIN)
        )
    observed_errors = errors
    observed_complete = bool(
        hooks.build_state["ran"] and hooks.outcome_comparable(errors)
    )
    if active_canary and not observed_complete:
        return CodegenFileStepResult(
            stop_reply=render_codegen_canary_stop(CodegenCanaryStop.BUILD_UNCERTAIN)
        )
    mine = [e for e in errors if name in e]
    masked = hooks.count_unreliable(errors)
    if existing and not mine and not masked and hooks.build_state["exit_ok"]:
        return CodegenFileStepResult(
            row={"name": name, "note": "already clean, not regenerated"}
        )

    best_code: str | None = existing or None
    best_total: Any = hooks.score(errors) if existing else None
    note = "unchanged"
    siblings = {n: hooks.read_file(n) for n, _ in wanted if n != name}
    siblings = {n: t for n, t in siblings.items() if t.strip()}
    progress_guard = VerificationProgressGuard()
    prior_candidate: str | None = None
    prior_diagnostics: list[str] | None = None
    prior_score: Any = (
        hooks.score(errors)
        if hooks.build_state["ran"] and hooks.outcome_comparable(errors)
        else None
    )
    prior_progress = "initial build"
    repair_notes: list[str] = []
    critic_attempted = False
    canary_next_action: StrategyAction = StrategyAction.REPAIR

    for attempt in range(1, attempt_limit + 1):
        if active_canary and clock.operation_deadline is not None:
            clock.attempt_deadline = min(
                clock.operation_deadline,
                time.monotonic() + plan.attempt_wall_ceiling,
            )
        prompt = "%s\n%s\nOutput only the contents of %s. Code only." % (
            hooks.dependency_brief(siblings), spec, name,
        )
        if prior_candidate is not None:
            prompt += hooks.repair_brief(
                prior_candidate, prior_diagnostics, prior_progress,
            )
        memory_exposed = False
        memory_model = ""
        memory_tier = ""
        memory_window = 0
        critic_this_attempt = bool(
            plan.repair_tiers
            and prior_candidate is not None
            and prior_diagnostics
            and hooks.outcome_comparable(prior_diagnostics)
            and (
                (active_canary and canary_next_action is StrategyAction.CRITIC)
                or (not active_canary and attempt == 3)
            )
        )
        rotated_this_attempt = bool(
            plan.repair_tiers
            and (
                (active_canary and canary_next_action is StrategyAction.SWITCH_MODEL)
                or (not active_canary and attempt == 4 and critic_attempted)
            )
        )
        if (
            strategy_memory is not None
            and prior_candidate is not None
            and (active_canary or len(explicit_targets) == 1)
            and prior_diagnostics
            and hooks.outcome_comparable(prior_diagnostics)
        ):
            memory_model = (
                plan.repair_models[1]
                if rotated_this_attempt and plan.repair_models
                else plan.repair_models[0]
                if plan.repair_models
                else explicit_targets[0][1]
            )
            memory_tier = (
                plan.repair_tiers[1]
                if rotated_this_attempt and plan.repair_tiers
                else plan.repair_tiers[0]
                if plan.repair_tiers
                else explicit_targets[0][0]
            )
            if hooks.is_local_ollama_route(memory_tier, memory_model):
                memory_window = hooks.local_num_ctx(memory_model)
                output_reserve = max(64, num_predict)
                prompt_cost = len(prompt.encode("utf-8")) + (
                    18150 if critic_this_attempt else 0
                )
                available_memory = memory_window - output_reserve - prompt_cost - 64
                if available_memory >= 320:
                    memory_tokens = min(512, available_memory // 2)
                    requests = dict.fromkeys(CONTEXT_SECTIONS, 0)
                    requests["working_files"] = prompt_cost
                    requests["memories"] = memory_tokens
                    try:
                        ctx_plan = ContextPlanner().plan(
                            ModelContext(memory_model, memory_window, output_reserve),
                            requests,
                            requests,
                        )
                        recovery = strategy_memory.recovery_context(
                            strategy_run_id,
                            hooks.attempt_id(name, attempt),
                            project_scope=project_dir,
                            plan=ctx_plan,
                            failure_class=FailureClass.BUILD_FAILURE,
                            family="patch",
                            language=language_from_path(name),
                        )
                        prompt += recovery.prompt_brief
                        memory_exposed = bool(recovery.prompt_brief)
                    except Exception as error:  # noqa: BLE001 - canary must not misattribute prior refs
                        _LOGGER.warning(
                            "codegen memory selection failed: %s", type(error).__name__,
                        )
                        if active_canary:
                            return CodegenFileStepResult(
                                stop_reply=render_codegen_canary_stop(
                                    CodegenCanaryStop.RESULT_UNSEALED
                                )
                            )
        if active_canary and (
            canary_next_action not in _ACTIVE_CANARY_ACTIONS
            or (canary_next_action is StrategyAction.CRITIC and not critic_this_attempt)
            or (
                canary_next_action is StrategyAction.SWITCH_MODEL
                and not rotated_this_attempt
            )
        ):
            note += f"; strategy canary paused before attempt {attempt}"
            break
        generation_tiers = (
            plan.repair_tiers[1]
            if rotated_this_attempt and plan.repair_tiers
            else plan.repair_tiers[0]
            if plan.repair_tiers
            else tiers
        )
        reserved_usage: StrategyUsage | None = None
        if active_canary:
            reserved_usage = build_reserved_usage(
                budget,
                critic_this_attempt=critic_this_attempt,
                rotated_this_attempt=rotated_this_attempt,
                attempt_wall_ceiling=plan.attempt_wall_ceiling,
            )
            try:
                if strategy_trace is None:
                    raise RuntimeError("sealed strategy trace unavailable")
                strategy_trace.reserve_next(
                    strategy_run_id,
                    hooks.attempt_id(name, attempt),
                    objective_digest=hooks.objective_digest_for(spec),
                    action=canary_next_action,
                    usage=reserved_usage,
                    budget=budget.canary_budget,
                )
            except Exception as error:  # noqa: BLE001 - no model dispatch without a seal
                _LOGGER.warning(
                    "codegen canary reservation failed: %s", type(error).__name__,
                )
                return CodegenFileStepResult(
                    stop_reply=render_codegen_canary_stop(CodegenCanaryStop.RESERVATION)
                )
        if critic_this_attempt and plan.repair_tiers and plan.repair_models and prior_candidate is not None and prior_diagnostics is not None:
            critic_attempted = True
            answered, diagnosis = hooks.run_critic(
                spec,
                prior_candidate,
                prior_diagnostics,
                name,
                hooks.dependency_brief(siblings),
                budget.critic_context_window,
                plan.repair_tiers,
                plan.repair_models,
            )
            if answered:
                prompt += (
                    "\n\nIndependent critic suggestion (untrusted; compiler remains "
                    "authoritative): " + json.dumps(diagnosis)
                )
                repair_notes.append("attempt 3 used scoped critic")
            else:
                repair_notes.append("attempt 3 critic unavailable")
        elif rotated_this_attempt:
            repair_notes.append("attempt 4 used alternate model tier")
        if active_canary:
            try:
                target_model = (
                    plan.repair_models[1]
                    if rotated_this_attempt and plan.repair_models
                    else plan.repair_models[0]
                    if plan.repair_models
                    else explicit_targets[0][1]
                )
                target_ctx = (
                    budget.alternate_context_window
                    if rotated_this_attempt
                    else budget.generation_context_window
                )
                reply = hooks.generate_pinned(prompt, target_model, target_ctx)
            except Exception as error:  # noqa: BLE001 - pending liability must remain after failure
                if hooks.is_cancelled_error(error):
                    raise
                return CodegenFileStepResult(
                    stop_reply=render_codegen_canary_stop(CodegenCanaryStop.MODEL_FAILURE)
                )
        elif memory_exposed:
            try:
                reply = hooks.generate_observed(
                    prompt, memory_model, memory_tier, memory_window,
                )
            except Exception as error:  # noqa: BLE001
                if hooks.is_model_call_error(error):
                    if hooks.is_cancelled_error(error):
                        raise
                    return CodegenFileStepResult(
                        stop_reply=hooks.format_model_call_error(error)
                    )
                raise
        else:
            reply = hooks.generate_ensemble(prompt, generation_tiers)
        code = hooks.strip_code(reply)
        code, hits = hooks.apply_slips(code, slips)

        if hooks.shrink_rejected(existing, code):
            decision = observe_candidate(
                attempt,
                code,
                observed_errors,
                observed_errors,
                before_complete=observed_complete,
                after_complete=observed_complete,
                route=generation_tiers,
                critic_used=critic_this_attempt,
                rotated=rotated_this_attempt,
                rejected=True,
                reserved_usage=reserved_usage,
                reserved_action=canary_next_action if active_canary else None,
            )
            if canary_observation_failed:
                return CodegenFileStepResult(
                    stop_reply=render_codegen_canary_stop(CodegenCanaryStop.RESULT_UNSEALED)
                )
            note = "attempt %d rejected: shrank to %d%% of the original" % (
                attempt, 100 * len(code) // max(1, len(existing)),
            )
            if active_canary and decision.action not in _ACTIVE_CANARY_ACTIONS:
                note += "; strategy canary paused"
                break
            if active_canary:
                canary_next_action = decision.action
            continue
        try:
            hooks.write_file(name, code)
        except Exception as exc:
            observe_candidate(
                attempt,
                code,
                observed_errors,
                observed_errors,
                before_complete=observed_complete,
                after_complete=False,
                route=generation_tiers,
                critic_used=critic_this_attempt,
                rotated=rotated_this_attempt,
                unresolved_effects=True,
                reserved_usage=reserved_usage,
                reserved_action=canary_next_action if active_canary else None,
            )
            return CodegenFileStepResult(write_failure=("write", name, exc))

        try:
            attempt_errors = hooks.run_build()
        except TimeoutError:
            if not active_canary:
                raise
            return CodegenFileStepResult(
                stop_reply=render_codegen_canary_stop(CodegenCanaryStop.BUILD_UNCERTAIN)
            )
        after_complete = bool(
            hooks.build_state["ran"] and hooks.outcome_comparable(attempt_errors)
        )
        no_progress = bool(
            attempt >= 2
            and observed_complete
            and after_complete
            and prior_diagnostics
            and outcome_fingerprint(observed_errors) == outcome_fingerprint(attempt_errors)
        )
        decision = observe_candidate(
            attempt,
            code,
            observed_errors,
            attempt_errors,
            before_complete=observed_complete,
            after_complete=after_complete,
            route=generation_tiers,
            critic_used=critic_this_attempt,
            rotated=rotated_this_attempt,
            no_progress=no_progress,
            reserved_usage=reserved_usage,
            reserved_action=canary_next_action if active_canary else None,
        )
        if canary_observation_failed:
            return CodegenFileStepResult(
                stop_reply=render_codegen_canary_stop(CodegenCanaryStop.RESULT_UNSEALED)
            )
        if active_canary and not after_complete:
            return CodegenFileStepResult(
                stop_reply=render_codegen_canary_stop(CodegenCanaryStop.BUILD_UNCERTAIN)
            )
        if active_canary:
            canary_next_action = decision.action
        observed_errors, observed_complete = attempt_errors, after_complete
        if not hooks.build_state["ran"]:
            note = "attempt %d abandoned: the build did not run" % attempt
            break
        attempt_score = hooks.score(attempt_errors)
        if best_total is None or attempt_score < best_total:
            best_code, best_total = code, attempt_score
            note = "attempt %d kept (%s%s)" % (
                attempt,
                hooks.describe_total(attempt_errors),
                ", %d slip(s) rewritten" % hits if hits else "",
            )
        if not attempt_errors:
            break
        comparable = hooks.outcome_comparable(attempt_errors)
        if comparable and prior_score is not None:
            prior_progress = (
                "improved"
                if attempt_score < prior_score
                else "regressed"
                if attempt_score > prior_score
                else "unchanged"
            )
        else:
            prior_progress = "unmeasured (build diagnostics incomplete)"
        prior_candidate = code
        prior_diagnostics = attempt_errors
        prior_score = attempt_score if comparable else None
        if active_canary and canary_next_action not in _ACTIVE_CANARY_ACTIONS:
            note += f"; strategy canary paused after attempt {attempt}"
            break
        if not comparable:
            progress_guard.reset()
        elif progress_guard.observe(attempt_errors, score=attempt_score):
            if plan.repair_tiers and attempt == 2 and attempt_limit >= 3:
                progress_guard.reset()
                continue
            note += (
                "; stopped after attempt %d: no progress (identical build "
                "errors on %d consecutive attempts, fingerprint %s) -- "
                "change the spec or approach instead of retrying"
                % (
                    attempt,
                    progress_guard.repeat_limit,
                    progress_guard.stalled_fingerprint,
                )
            )
            break
        if (
            plan.repair_tiers
            and attempt >= 4
            and attempt < attempt_limit
            and prior_progress != "improved"
        ):
            note += (
                f"; stopped after attempt {attempt}: alternate model showed no "
                "measured build improvement"
            )
            break

    if best_code is not None:
        try:
            hooks.write_file(name, best_code)
        except Exception as exc:
            return CodegenFileStepResult(write_failure=("restore", name, exc))
    clock.attempt_deadline = None
    if repair_notes:
        note += "; " + "; ".join(repair_notes)
    return CodegenFileStepResult(row={"name": name, "note": note})
