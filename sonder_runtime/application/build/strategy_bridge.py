"""Map build-fix attempts onto the host strategy controller (F11).

Two progress readings exist on purpose and both are recorded:

* the loop's keep-best rule is lexicographic (``repair.progress_key``): a
  candidate that removes compile errors but adds a warning is *better*;
* the strategy controller sees ``to_strategy_metrics`` as a
  ``domain.strategy.ProgressVector`` and judges it by dominance
  (``assess_progress``): any metric that got worse makes it REGRESSED.

The controller therefore chooses ROLLBACK/INSPECT for "errors fixed, warning
added" even though the loop keeps that candidate as its best. Nothing here
executes an effect: a decision is only advice the loop acts on.

``StrategyAction`` values map to fix actions as REPAIR, INSPECT (also
RETRIEVE), CRITIC, SWITCH_MODEL, ROLLBACK and FAIL; PAUSE, RECONCILE and
every other action become ``fail`` carrying the controller's reason, since a
fix job has nobody to pause for.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any

from ...domain.build.repair import BuildProgress, progress_key, to_strategy_metrics
from ...domain.strategy.models import (
    FailureClass,
    FailureObservation,
    ProgressAssessment,
    ProgressMetric,
    ProgressVector,
    StrategyAction,
    StrategyAttempt,
    StrategyBudget,
    StrategyError,
    StrategySignature,
    StrategyUsage,
    assess_progress,
)
from ..strategy.controller import StrategyController, StrategyState
from .fix_ports import FixDecision

FAILURE_CLASSES = {
    "BUILD_FAILURE": FailureClass.BUILD_FAILURE,
    "NO_PROGRESS": FailureClass.NO_PROGRESS,
    "ENVIRONMENT_FAILURE": FailureClass.ENVIRONMENT_FAILURE,
    "VERIFIER_FAILURE": FailureClass.VERIFIER_FAILURE,
    "HYPOTHESIS_REJECTED": FailureClass.HYPOTHESIS_REJECTED,
    "UNCERTAIN_SIDE_EFFECT": FailureClass.UNCERTAIN_SIDE_EFFECT,
    "PERMISSION_DENIED": FailureClass.PERMISSION_DENIED,
}

_ACTION_MAP = {
    StrategyAction.REPAIR: "repair",
    StrategyAction.INSPECT: "inspect",
    StrategyAction.RETRIEVE: "inspect",
    StrategyAction.CRITIC: "critic",
    StrategyAction.SWITCH_MODEL: "switch_model",
    StrategyAction.ROLLBACK: "rollback",
    StrategyAction.FAIL: "fail",
}
_MAX_HISTORY = 64


def _hex(value: Any) -> str:
    material = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def strategy_vector(progress: BuildProgress | None, scope_digest: str) -> ProgressVector | None:
    """The controller's view of one measurement; None when the build did not run."""
    if progress is None or not progress.build_ran:
        return None
    metrics = tuple(ProgressMetric(name, float(value)) for name, value in to_strategy_metrics(progress))
    return ProgressVector(scope_digest, metrics, complete=bool(progress.complete))


def lexicographic_assessment(before: BuildProgress | None, after: BuildProgress | None) -> str:
    if before is None or after is None:
        return ProgressAssessment.INCOMPARABLE.value
    left, right = progress_key(before), progress_key(after)
    if right < left:
        return ProgressAssessment.IMPROVED.value
    if right > left:
        return ProgressAssessment.REGRESSED.value
    return ProgressAssessment.NEUTRAL.value


@dataclass(frozen=True)
class ObservationRecord:
    """One observed attempt: what each reading said and what was decided."""

    attempt: int
    failure: str
    dominance: str
    lexicographic: str
    controller_action: str
    action: str
    reason: str


class StrategyFixAdapter:
    """``FixStrategyPort`` over ``application.strategy.controller.StrategyController``.

    One adapter instance serves one fix run (``begin`` resets it); the fix
    service asks its factory for a fresh adapter per job.
    """

    def __init__(self, controller: StrategyController | None = None, *,
                 switch_model_available: bool = False, critic_available: bool = True) -> None:
        self._controller = controller or StrategyController()
        self._switch_model = bool(switch_model_available)
        self._critic = bool(critic_available)
        self._lock = threading.Lock()
        self._run_id = ""
        self._objective = ""
        self._history: list[StrategyAttempt] = []
        self._usage = StrategyUsage()
        self._budget = StrategyBudget()
        self._records: list[ObservationRecord] = []

    @property
    def records(self) -> tuple[ObservationRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def available_actions(self) -> tuple[StrategyAction, ...]:
        actions = [StrategyAction.REPAIR, StrategyAction.INSPECT, StrategyAction.ROLLBACK]
        if self._critic:
            actions.append(StrategyAction.CRITIC)
        if self._switch_model:
            actions.append(StrategyAction.SWITCH_MODEL)
        return tuple(actions)

    def begin(self, run_id: str, objective_digest: str, *, attempts: int = 4,
              max_model_calls: int = 12, wall_seconds: float = 3600.0) -> None:
        if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 128:
            raise StrategyError("run_id must be bounded text")
        with self._lock:
            self._run_id = run_id
            self._objective = objective_digest if isinstance(objective_digest, str) \
                and len(objective_digest) == 64 else _hex({"objective": objective_digest})
            self._history = []
            self._records = []
            self._usage = StrategyUsage()
            # The loop enforces its own attempt count first; the controller's
            # budget sits one above it so it never preempts that stop reason.
            self._budget = StrategyBudget(
                attempts=int(attempts) + 1, model_calls=max(1, int(max_model_calls)),
                tool_calls=256, verifier_calls=max(4, int(attempts) * 3 + 3),
                tokens=10 ** 9, wall_seconds=max(1.0, float(wall_seconds)),
                descendants=8, strategy_switches=4, replans=2, critic_calls=2, top_tier_calls=1,
            )

    def observe(self, attempt: Any, *, before: BuildProgress | None, after: BuildProgress | None,
                failure: str | None, hypothesis_digest: str = "", focus: str = "",
                model_calls: int = 0, verifier_calls: int = 0) -> FixDecision:
        with self._lock:
            if not self._run_id:
                raise StrategyError("begin() must be called before observe()")
            n = int(getattr(attempt, "n", len(self._history) + 1))
            failure_obs = None
            if failure:
                klass = FAILURE_CLASSES.get(str(failure).upper())
                if klass is None:
                    raise StrategyError("unknown fix failure class %r" % failure)
                failure_obs = FailureObservation(klass, source_code="build_fix:%s" % str(failure).lower())
            fixed = bool(after is not None and after.fixed and failure_obs is None)
            outcome = "succeeded" if fixed else (
                "uncertain" if failure_obs is not None and failure_obs.requires_reconciliation
                else "failed")
            if outcome == "failed" and failure_obs is None:
                failure_obs = FailureObservation(FailureClass.BUILD_FAILURE,
                                                 source_code="build_fix:build_failure")
            vec_before = strategy_vector(before, self._objective)
            vec_after = strategy_vector(after, self._objective)
            signature = StrategySignature(
                family="patch",
                objective_digest=self._objective,
                target_scope=((str(focus) or "target")[:500],),
                hypothesis_digest=hypothesis_digest if isinstance(hypothesis_digest, str)
                and len(hypothesis_digest) == 64 else _hex({"attempt": n, "h": hypothesis_digest}),
                intended_change="repair compile errors in %s" % ((str(focus) or "the target")[:200]),
                verifier_target="build:%s" % self._run_id[:100],
            )
            usage = StrategyUsage(attempts=1, model_calls=max(0, int(model_calls)),
                                  tokens=max(0, int(model_calls)),
                                  verifier_calls=max(0, int(verifier_calls)))
            record = StrategyAttempt(
                run_id=self._run_id, attempt_id="%s:%d:%d" % (self._run_id[:100], n, len(self._history)),
                signature=signature, outcome=outcome,
                failure=None if outcome == "succeeded" else failure_obs,
                progress_before=vec_before, progress_after=vec_after, usage=usage,
            )
            self._history.append(record)
            self._history = self._history[-_MAX_HISTORY:]
            self._usage = self._usage.plus(usage)
            dominance = assess_progress(vec_before, vec_after).value
            lexicographic = lexicographic_assessment(before, after)
            state = StrategyState(
                objective_digest=self._objective,
                history=tuple(self._history),
                failure=None if outcome == "succeeded" else failure_obs,
                budget=self._budget,
                usage=self._usage,
                available_actions=self.available_actions(),
            )
            decision = self._controller.decide(state)
            action = _ACTION_MAP.get(decision.action, "fail")
            reason = decision.reason
            if action == "fail" and decision.action is not StrategyAction.FAIL:
                reason = "%s:%s" % (decision.action.value, decision.reason)
            result = FixDecision(action=action, reason=reason,
                                 controller_action=decision.action.value,
                                 dominance=dominance, lexicographic=lexicographic)
            self._records.append(ObservationRecord(
                attempt=n, failure=str(failure or ""), dominance=dominance,
                lexicographic=lexicographic, controller_action=decision.action.value,
                action=action, reason=reason,
            ))
            return result


__all__ = [
    "FAILURE_CLASSES", "ObservationRecord", "StrategyFixAdapter", "lexicographic_assessment",
    "strategy_vector",
]
