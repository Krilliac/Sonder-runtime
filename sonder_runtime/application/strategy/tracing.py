"""Observe strategy decisions using the existing sealed runtime checkpoint port.

The service never executes a proposed action. Every append is one generation
CAS, keeps existing checkpoint facets, and fails closed on ambiguous/corrupt
history. The checkpoint adapter remains responsible for effect high-water
coupling and crash reconciliation; this is not another persistence system.
"""
from __future__ import annotations

import json
from dataclasses import asdict, replace

from sonder_runtime.application.ports.runtime_checkpoints import (
    CheckpointError,
    RestoreStatus,
    RuntimeCheckpoint,
    RuntimeCheckpointRepository,
    canonical_json,
)
from sonder_runtime.application.strategy.controller import (
    StrategyController,
    StrategyDecision,
    StrategyState,
)
from sonder_runtime.domain.strategy.models import (
    ProgressAssessment,
    StrategyAction,
    StrategyAttempt,
    StrategyBudget,
    StrategyError,
    StrategyUsage,
)


class StrategyTraceService:
    def __init__(self, repository: RuntimeCheckpointRepository, controller: StrategyController | None = None):
        self._repository = repository
        self._controller = controller or StrategyController()

    def _restore(self, run_id):
        restored = self._repository.restore(run_id)
        if restored.status is RestoreStatus.EMPTY:
            return None, None
        if restored.status is not RestoreStatus.RESTORED or restored.checkpoint is None:
            raise CheckpointError("strategy history cannot be restored safely")
        checkpoint = restored.checkpoint
        raw = checkpoint.decisions.get("strategy_v1")
        if raw is None:
            return checkpoint, None
        try:
            data = json.loads(canonical_json({"state": raw}))["state"]
            if set(data) != {"schema", "mode", "objective_digest", "budget", "attempts", "decisions"} or data["schema"] != 1 or data["mode"] != "observe":
                raise StrategyError("unsupported strategy checkpoint")
            budget = StrategyBudget(**data["budget"])
            history = tuple(StrategyAttempt.from_dict(x) for x in data["attempts"])
            if set(data["decisions"]) != {x.attempt_id for x in history}:
                raise StrategyError("strategy decisions do not match attempts")
            for decision in data["decisions"].values():
                self._decision(decision)
            if any(x.run_id != run_id for x in history):
                raise StrategyError("strategy run mismatch")
            StrategyState(data["objective_digest"], history=history, budget=budget)
            return checkpoint, data
        except (TypeError, ValueError, KeyError) as exc:
            raise CheckpointError("invalid strategy checkpoint state") from exc

    @staticmethod
    def _decision(raw):
        if set(raw) != {"action", "reason", "progress", "policy_version"}:
            raise StrategyError("exact strategy decision required")
        return StrategyDecision(StrategyAction(raw["action"]), raw["reason"],
                                ProgressAssessment(raw["progress"]), raw["policy_version"])

    def history(self, run_id: str) -> tuple[StrategyAttempt, ...]:
        _, data = self._restore(run_id)
        return () if data is None else tuple(StrategyAttempt.from_dict(x) for x in data["attempts"])

    def observed_decisions(self, run_id: str) -> tuple[StrategyDecision, ...]:
        """Read decisions in attempt order from the authenticated checkpoint."""
        _, data = self._restore(run_id)
        if data is None:
            return ()
        return tuple(self._decision(data["decisions"][entry["attempt_id"]])
                     for entry in data["attempts"])

    def record(self, attempt: StrategyAttempt, *, budget: StrategyBudget,
               available_actions: tuple[StrategyAction, ...], unresolved_effects: bool = False,
               policy_blocked: bool = False, artifacts_ready: bool = True,
               transport_replay_safe: bool = False) -> StrategyDecision:
        if not isinstance(attempt, StrategyAttempt) or not isinstance(budget, StrategyBudget):
            raise StrategyError("typed strategy attempt and budget required")
        if attempt.usage.attempts != 1:
            raise StrategyError("each durable strategy attempt must charge exactly one attempt")
        checkpoint, data = self._restore(attempt.run_id)
        entries = [] if data is None else data["attempts"]
        history = tuple(StrategyAttempt.from_dict(x) for x in entries)
        replay = False
        if data is not None:
            original_budget = StrategyBudget(**data["budget"])
            if not original_budget.allows(budget):
                raise StrategyError("persisted strategy budget cannot expand")
            for old in history:
                if old.attempt_id == attempt.attempt_id:
                    if old != attempt:
                        raise StrategyError("strategy attempt identity reused with different content")
                    replay = True
                    break
        if not replay:
            history += (attempt,)
        usage = StrategyUsage()
        for item in history:
            usage = usage.plus(item.usage)
        state = StrategyState(
            attempt.signature.objective_digest, history=history, failure=history[-1].failure,
            budget=budget, usage=usage, available_actions=available_actions,
            unresolved_effects=unresolved_effects, policy_blocked=policy_blocked,
            artifacts_ready=artifacts_ready, transport_replay_safe=transport_replay_safe,
        )
        decision = self._controller.decide(state)
        if replay:
            # A decision in the checkpoint describes what the host observed at
            # that time. Replays use current host facts, capabilities and budget;
            # an older attempt must not resurrect an action after later work.
            if history[-1].attempt_id != attempt.attempt_id and decision.action not in {
                    StrategyAction.RECONCILE, StrategyAction.PAUSE, StrategyAction.FAIL}:
                decision = StrategyDecision(StrategyAction.PAUSE, "historical_attempt_already_recorded")
            if budget != original_budget:
                narrowed = {**data, "budget": asdict(budget)}
                checkpoint = replace(
                    checkpoint, generation=checkpoint.generation + 1,
                    decisions={**checkpoint.decisions, "strategy_v1": narrowed},
                )
                self._repository.save(checkpoint, expected_generation=checkpoint.generation - 1)
            return decision
        strategy = {"schema": 1, "mode": "observe", "objective_digest": state.objective_digest,
                    "budget": asdict(budget),
                    "attempts": [*entries, attempt.as_dict()],
                    "decisions": {**({} if data is None else data["decisions"]), attempt.attempt_id: asdict(decision)}}
        expected = -1 if checkpoint is None else checkpoint.generation
        if checkpoint is None:
            checkpoint = RuntimeCheckpoint(attempt.run_id, 0, {"strategy_schema": 1})
        checkpoint = replace(checkpoint, generation=expected + 1,
                             decisions={**checkpoint.decisions, "strategy_v1": strategy})
        self._repository.save(checkpoint, expected_generation=expected)
        return decision
