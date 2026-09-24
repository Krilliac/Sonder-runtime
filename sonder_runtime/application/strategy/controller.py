"""Deterministic recovery selection; decisions never execute effects themselves."""
from dataclasses import dataclass, field

from sonder_runtime.domain.strategy.models import (
    FailureClass,
    FailureObservation,
    ProgressAssessment,
    StrategyAction,
    StrategyAttempt,
    StrategyBudget,
    StrategyError,
    StrategyUsage,
    assess_progress,
)


@dataclass(frozen=True, slots=True)
class StrategyState:
    objective_digest: str
    history: tuple[StrategyAttempt, ...] = ()
    failure: FailureObservation | None = None
    budget: StrategyBudget = field(default_factory=StrategyBudget)
    usage: StrategyUsage = field(default_factory=StrategyUsage)
    available_actions: tuple[StrategyAction, ...] = ()
    unresolved_effects: bool = False
    policy_blocked: bool = False
    artifacts_ready: bool = True
    transport_replay_safe: bool = False

    def __post_init__(self):
        from sonder_runtime.domain.strategy.models import _digest
        _digest(self.objective_digest)
        if type(self.history) is not tuple or len(self.history) > 64:
            raise StrategyError("bounded strategy history required")
        if any(not isinstance(x, StrategyAttempt) or x.signature.objective_digest != self.objective_digest for x in self.history):
            raise StrategyError("strategy history objective mismatch")
        if len({x.attempt_id for x in self.history}) != len(self.history):
            raise StrategyError("strategy attempts must be unique")
        if type(self.available_actions) is not tuple or any(not isinstance(x, StrategyAction) for x in self.available_actions):
            raise StrategyError("typed available actions required")
        if len(set(self.available_actions)) != len(self.available_actions):
            raise StrategyError("available actions must be unique")
        if not isinstance(self.budget, StrategyBudget) or not isinstance(self.usage, StrategyUsage):
            raise StrategyError("typed strategy resource state required")
        if self.failure is not None and not isinstance(self.failure, FailureObservation):
            raise StrategyError("typed failure required")
        for name in ("unresolved_effects", "policy_blocked", "artifacts_ready", "transport_replay_safe"):
            if type(getattr(self, name)) is not bool:
                raise StrategyError("strategy host facts must be boolean")


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    action: StrategyAction
    reason: str
    progress: ProgressAssessment = ProgressAssessment.INCOMPARABLE
    policy_version: str = "strategy-v1"


_MINIMUM_ACTION_USAGE = {
    # These are lower bounds for choosing a next attempt, not reservations or
    # permission to execute it. Route-specific top-tier and verifier costs
    # still require the host to filter available_actions before invocation.
    StrategyAction.RETRY_TRANSIENT: StrategyUsage(attempts=1, model_calls=1, tokens=1),
    StrategyAction.REPAIR: StrategyUsage(attempts=1, model_calls=1, tokens=1),
    StrategyAction.CRITIC: StrategyUsage(attempts=1, model_calls=1, tokens=1, critic_calls=1),
    StrategyAction.REPLAN: StrategyUsage(attempts=1, model_calls=1, tokens=1, replans=1),
    StrategyAction.SWITCH_MODEL: StrategyUsage(attempts=1, model_calls=1, tokens=1,
                                               strategy_switches=1),
    StrategyAction.SWITCH_TOOL: StrategyUsage(attempts=1, tool_calls=1,
                                              strategy_switches=1),
    StrategyAction.SPAWN_SPECIALIST: StrategyUsage(attempts=1, model_calls=1, tokens=1,
                                                   descendants=1),
    StrategyAction.PARALLEL_HYPOTHESES: StrategyUsage(attempts=1, model_calls=2, tokens=2,
                                                      descendants=2),
}


class StrategyController:
    """Host policy first. Available actions come from host capability/policy gates."""

    def decide(self, state: StrategyState) -> StrategyDecision:
        if not isinstance(state, StrategyState):
            raise StrategyError("typed strategy state required")
        failure = state.failure
        if state.unresolved_effects or (failure and failure.requires_reconciliation):
            return StrategyDecision(StrategyAction.RECONCILE, "unresolved_effects")
        if state.policy_blocked or (failure and failure.classification in {
                FailureClass.POLICY_BLOCK, FailureClass.PERMISSION_DENIED, FailureClass.OPERATOR_REQUIRED}):
            return StrategyDecision(StrategyAction.PAUSE, "host_policy_requires_operator")
        if not state.budget.allows(state.usage):
            return StrategyDecision(StrategyAction.FAIL, "strategy_budget_exhausted")
        if state.usage.wall_seconds >= state.budget.wall_seconds:
            return StrategyDecision(StrategyAction.FAIL, "wall_budget_exhausted")
        if not state.artifacts_ready:
            return StrategyDecision(StrategyAction.PAUSE, "artifact_not_ready")
        latest = state.history[-1] if state.history else None
        if latest is not None and latest.outcome == "succeeded" and failure is None:
            return StrategyDecision(StrategyAction.PAUSE, "attempt_succeeded_await_completion_gate")
        if state.usage.attempts >= state.budget.attempts:
            return StrategyDecision(StrategyAction.FAIL, "strategy_budget_exhausted")
        progress = assess_progress(latest.progress_before, latest.progress_after) if latest else ProgressAssessment.INCOMPARABLE
        stalled = False
        if latest and progress in {ProgressAssessment.NEUTRAL, ProgressAssessment.REGRESSED}:
            streak = 0
            for attempt in reversed(state.history):
                if (attempt.outcome == "succeeded" or attempt.signature.digest != latest.signature.digest
                        or assess_progress(attempt.progress_before, attempt.progress_after) is ProgressAssessment.IMPROVED):
                    break
                streak += 1
            stalled = streak >= 2
        if failure and failure.classification in {FailureClass.DUPLICATE_STRATEGY, FailureClass.NO_PROGRESS}:
            stalled = True
        if stalled:
            choices = (StrategyAction.CRITIC, StrategyAction.SWITCH_TOOL, StrategyAction.SWITCH_MODEL,
                       StrategyAction.SPAWN_SPECIALIST, StrategyAction.PARALLEL_HYPOTHESES, StrategyAction.REPLAN)
        elif failure and failure.transport_retryable:
            choices = (StrategyAction.RETRY_TRANSIENT,) if state.transport_replay_safe else (StrategyAction.INSPECT,)
        elif failure and failure.requires_reinspection:
            choices = (StrategyAction.INSPECT, StrategyAction.RETRIEVE)
        elif progress is ProgressAssessment.REGRESSED:
            choices = (StrategyAction.ROLLBACK, StrategyAction.INSPECT)
        elif failure and failure.classification in {FailureClass.TIME_BUDGET, FailureClass.RESOURCE_PRESSURE}:
            return StrategyDecision(StrategyAction.PAUSE, "resource_recovery_required", progress)
        elif failure and failure.classification in {FailureClass.BUILD_FAILURE, FailureClass.TEST_FAILURE,
                FailureClass.VERIFIER_FAILURE, FailureClass.IMPLEMENTATION_FAILURE}:
            choices = (StrategyAction.REPAIR, StrategyAction.INSPECT)
        elif failure and failure.classification is FailureClass.MODEL_UNAVAILABLE:
            choices = (StrategyAction.SWITCH_MODEL, StrategyAction.PAUSE)
        else:
            choices = (StrategyAction.INSPECT, StrategyAction.RETRIEVE)
        remaining = state.budget.remaining(state.usage)
        for action in choices:
            if action not in state.available_actions:
                continue
            required = _MINIMUM_ACTION_USAGE.get(action, StrategyUsage(attempts=1))
            if not remaining.allows(required):
                continue
            return StrategyDecision(action, "materially_different_strategy" if stalled else "host_failure_policy", progress)
        return StrategyDecision(StrategyAction.PAUSE, "no_admissible_strategy", progress)
