"""Deterministic strategy-policy canaries over the host's sealed trace service.

These are synthetic, held-out policy cases. They test controller decisions and
checkpoint integrity; they are not measurements of live task completion or an
independent grader for a candidate with access to the expected actions.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from types import MappingProxyType

from sonder_runtime.application.strategy.tracing import StrategyTraceService
from sonder_runtime.domain.strategy.models import (
    StrategyAction,
    StrategyAttempt,
    StrategyBudget,
    StrategyError,
    StrategyUsage,
)

from .proposal_lifecycle import (
    EvaluationDimension,
    EvaluationMode,
    EvaluationResult,
    EvaluationSuite,
)

_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_POLICY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_METRICS = tuple(sorted((
    "pass_rate", "task_success", "first_attempt_success", "repair_success",
    "attempts", "model_calls", "tool_calls", "verifier_calls", "tokens",
    "wall_seconds", "descendants", "strategy_switches", "replans",
    "critic_calls", "top_tier_calls",
)))


class StrategyEvidenceClass(str, Enum):
    """What the evaluator actually measured, separate from task success."""

    SYNTHETIC_POLICY_CANARY = "synthetic_policy_canary"


def strategy_evidence_class(result: EvaluationResult) -> StrategyEvidenceClass:
    """Require one recognized, unambiguous source class on a strategy result."""
    prefix = "strategy_evidence_class:"
    claims = tuple(item[len(prefix):] for item in result.provenance if item.startswith(prefix))
    if len(claims) != 1:
        raise StrategyError("strategy evaluation needs exactly one typed evidence class")
    try:
        return StrategyEvidenceClass(claims[0])
    except ValueError as error:
        raise StrategyError("strategy evaluation evidence class is unsupported") from error


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class StrategyEvaluationIdentity:
    """Host-supplied policy and runtime identities; digests are not attestation."""

    policy_version: str
    model_roles: Mapping[str, str]
    tool_visibility_digest: str
    memory_selection_digest: str
    skill_catalog_digest: str
    runtime_environment_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not _POLICY.fullmatch(self.policy_version):
            raise StrategyError("strategy policy version is invalid")
        if not isinstance(self.model_roles, Mapping) or not 1 <= len(self.model_roles) <= 16:
            raise StrategyError("bounded model role mapping is required")
        clean: dict[str, str] = {}
        for role, model in self.model_roles.items():
            if (not isinstance(role, str) or not _POLICY.fullmatch(role)
                    or not isinstance(model, str) or not model.strip()
                    or len(model) > 128 or any(ord(char) < 32 for char in model)):
                raise StrategyError("model role mapping is malformed")
            clean[role] = model.strip()
        object.__setattr__(self, "model_roles", MappingProxyType(dict(sorted(clean.items()))))
        for name in ("tool_visibility_digest", "memory_selection_digest",
                     "skill_catalog_digest", "runtime_environment_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise StrategyError(f"{name} must be a SHA-256 identity")

    @property
    def digest(self) -> str:
        return _hash({
            "policy_version": self.policy_version,
            "model_roles": dict(self.model_roles),
            "tool_visibility_digest": self.tool_visibility_digest,
            "memory_selection_digest": self.memory_selection_digest,
            "skill_catalog_digest": self.skill_catalog_digest,
            "runtime_environment_digest": self.runtime_environment_digest,
        })

    def suite(self) -> EvaluationSuite:
        dimensions = {
            "policy_version": self.policy_version,
            "model_roles": _hash(dict(self.model_roles)),
            "tool_visibility": self.tool_visibility_digest,
            "memory_selection": self.memory_selection_digest,
            "skill_catalog": self.skill_catalog_digest,
            "runtime_environment": self.runtime_environment_digest,
            "split": "heldout",
        }
        return EvaluationSuite(
            "strategy-orchestration", self.policy_version,
            tuple(EvaluationDimension(name, value) for name, value in sorted(dimensions.items())),
            _METRICS,
        )


@dataclass(frozen=True, slots=True)
class StrategyHeldoutStep:
    attempt: StrategyAttempt
    budget: StrategyBudget
    available_actions: tuple[StrategyAction, ...]
    expected_action: StrategyAction
    unresolved_effects: bool = False
    policy_blocked: bool = False
    artifacts_ready: bool = True
    transport_replay_safe: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, StrategyAttempt) or not isinstance(self.budget, StrategyBudget):
            raise StrategyError("held-out step needs a typed attempt and budget")
        if type(self.available_actions) is not tuple or any(
            not isinstance(item, StrategyAction) for item in self.available_actions
        ) or not isinstance(self.expected_action, StrategyAction):
            raise StrategyError("held-out step needs typed host actions")
        for key in ("unresolved_effects", "policy_blocked", "artifacts_ready", "transport_replay_safe"):
            if type(getattr(self, key)) is not bool:
                raise StrategyError("held-out host facts must be boolean")


@dataclass(frozen=True, slots=True)
class StrategyHeldoutCase:
    case_id: str
    steps: tuple[StrategyHeldoutStep, ...]
    evidence_class: StrategyEvidenceClass = StrategyEvidenceClass.SYNTHETIC_POLICY_CANARY

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not _POLICY.fullmatch(self.case_id):
            raise StrategyError("held-out case ID is invalid")
        if self.evidence_class is not StrategyEvidenceClass.SYNTHETIC_POLICY_CANARY:
            raise StrategyError("held-out policy cases cannot assert real task measurements")
        if type(self.steps) is not tuple or not 1 <= len(self.steps) <= 16 or any(
            not isinstance(step, StrategyHeldoutStep) for step in self.steps
        ):
            raise StrategyError("held-out case must contain 1..16 typed steps")
        ids = [step.attempt.attempt_id for step in self.steps]
        if len(set(ids)) != len(ids) or len({step.attempt.run_id for step in self.steps}) != 1:
            raise StrategyError("held-out steps need a unique attempt graph for one run")

    @property
    def source_digest(self) -> str:
        """Input/case identity excludes measured outcomes and resource usage."""
        return _hash({
            "case_id": self.case_id, "evidence_class": self.evidence_class.value,
            "steps": [
                {"signature": asdict(step.attempt.signature),
                 "before": (asdict(step.attempt.progress_before)
                            if step.attempt.progress_before is not None else None),
                 "budget": asdict(step.budget),
                 "available_actions": [action.value for action in step.available_actions],
                 "expected_action": step.expected_action.value,
                 "unresolved_effects": step.unresolved_effects,
                 "policy_blocked": step.policy_blocked,
                 "artifacts_ready": step.artifacts_ready,
                 "transport_replay_safe": step.transport_replay_safe}
                for step in self.steps
            ],
        })


def _attempt_graph_digest(history: tuple[StrategyAttempt, ...]) -> str:
    prior: set[str] = set()
    graph = []
    for attempt in history:
        if attempt.attempt_id in prior or (
            attempt.parent_attempt_id and attempt.parent_attempt_id not in prior
        ):
            raise StrategyError("strategy attempt graph is duplicate or has an absent parent")
        graph.append({"attempt_id": attempt.attempt_id,
                      "parent_id": attempt.parent_attempt_id, "attempt_digest": attempt.digest})
        prior.add(attempt.attempt_id)
    if not graph:
        raise StrategyError("strategy evaluation cannot use an empty attempt graph")
    return _hash(graph)


def evaluate_heldout_strategy_case(
    case: StrategyHeldoutCase,
    identity: StrategyEvaluationIdentity,
    *,
    trace_factory: Callable[[], StrategyTraceService],
    candidate: str,
    baseline: str,
) -> EvaluationResult:
    """Run the same host-owned case twice in distinct empty sealed checkpoints."""
    if not isinstance(case, StrategyHeldoutCase) or not isinstance(identity, StrategyEvaluationIdentity):
        raise StrategyError("typed strategy case and identity required")
    run_id = case.steps[0].attempt.run_id
    case_digest = _hash(asdict(case))
    runs: list[tuple[tuple[StrategyAction, ...], str, tuple[StrategyAttempt, ...]]] = []
    first_repository = None
    for _ in range(2):
        trace = trace_factory()
        if not isinstance(trace, StrategyTraceService) or trace._repository is first_repository:
            raise StrategyError("held-out replay needs independent trace repositories")
        if trace.history(run_id):
            raise StrategyError("held-out replay needs a fresh checkpoint")
        if first_repository is None:
            first_repository = trace._repository
        observed = []
        for step in case.steps:
            decision = trace.record(
                step.attempt, budget=step.budget,
                available_actions=step.available_actions,
                unresolved_effects=step.unresolved_effects,
                policy_blocked=step.policy_blocked,
                artifacts_ready=step.artifacts_ready,
                transport_replay_safe=step.transport_replay_safe,
            )
            if decision.policy_version != identity.policy_version:
                raise StrategyError("strategy policy version differs from observed controller")
            observed.append(decision.action)
        decisions = trace.observed_decisions(run_id)
        if tuple(item.action for item in decisions) != tuple(observed):
            raise StrategyError("strategy trace decisions do not match the recorded run")
        history = trace.history(run_id)
        if history != tuple(step.attempt for step in case.steps):
            raise StrategyError("strategy trace attempt graph does not match host case")
        runs.append((tuple(observed), _attempt_graph_digest(history), history))
    actions, graph_digest, history = runs[0]
    replay_equivalent = runs[0][:2] == runs[1][:2]
    passed = replay_equivalent and actions == tuple(step.expected_action for step in case.steps)
    usage = StrategyUsage()
    for attempt in history:
        usage = usage.plus(attempt.usage)
    metrics: dict[str, float] = {
        name: float(getattr(usage, name))
        for name in ("attempts", "model_calls", "tool_calls", "verifier_calls", "tokens",
                     "wall_seconds", "descendants", "strategy_switches", "replans",
                     "critic_calls", "top_tier_calls")
    }
    metrics.update({
        # The synthetic attempt's claimed outcome is input to this canary,
        # not a receipt from an independently completed user task.
        "pass_rate": float(passed), "task_success": 0.0,
        "first_attempt_success": 0.0, "repair_success": 0.0,
    })
    return EvaluationResult(
        f"strategy/{case.case_id}/{graph_digest[:16]}", identity.suite(),
        candidate, baseline, EvaluationMode.OFFLINE, identity.suite().dimensions,
        metrics, passed, 1, trajectory_digest=graph_digest,
        replay_equivalent=replay_equivalent,
        provenance=(f"case:{case.case_id}", f"attempt_graph:{graph_digest}",
                    f"case_digest:{case_digest}",
                    f"case_source_digest:{case.source_digest}",
                    f"strategy_evidence_class:{case.evidence_class.value}",
                    f"strategy_identity:{identity.digest}", f"trace_run:{run_id}"),
    )


def compare_strategy_ablations(left: EvaluationResult, right: EvaluationResult) -> Mapping[str, float]:
    """Return observed differences; paired synthetic runs imply no causal lift."""
    if left.suite.suite_id != "strategy-orchestration" or right.suite.suite_id != "strategy-orchestration":
        raise StrategyError("strategy ablation requires strategy evaluation results")
    if left.suite.version != right.suite.version:
        raise StrategyError("strategy ablation policy versions differ")
    left_case = next((item for item in left.provenance if item.startswith("case:")), "")
    right_case = next((item for item in right.provenance if item.startswith("case:")), "")
    if not left_case or left_case != right_case:
        raise StrategyError("ablation comparisons need the same held-out case")
    left_source = next((item for item in left.provenance if item.startswith("case_source_digest:")), "")
    right_source = next((item for item in right.provenance if item.startswith("case_source_digest:")), "")
    if not left_source or left_source != right_source:
        raise StrategyError("ablation comparisons need the same held-out source")
    if not left.replay_equivalent or not right.replay_equivalent:
        raise StrategyError("ablation comparison needs reproducible traces")
    fixed = {item.name: item.value for item in left.dimensions}
    other = {item.name: item.value for item in right.dimensions}
    for name in ("split", "tool_visibility", "memory_selection", "skill_catalog", "runtime_environment"):
        if fixed[name] != other[name]:
            raise StrategyError("ablation runtime and visible context differ")
    return MappingProxyType({name: right.metrics[name] - left.metrics[name] for name in _METRICS})


__all__ = [
    "StrategyEvaluationIdentity",
    "StrategyEvidenceClass",
    "StrategyHeldoutCase",
    "StrategyHeldoutStep",
    "compare_strategy_ablations",
    "evaluate_heldout_strategy_case",
    "strategy_evidence_class",
]
