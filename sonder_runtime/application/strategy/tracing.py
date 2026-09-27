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
    CheckpointConflict,
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
    _digest,
)

_PENDING_KEY = "strategy_pending_v1"
_SCOPE_GUARD_KEY = "strategy_scope_guard_v1"


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
        self._pending_from(checkpoint)
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

    def sealed_budget(self, run_id: str) -> StrategyBudget | None:
        """Return the authenticated budget a run was sealed with, if any.

        A recorded budget may only narrow, so observers use this to avoid
        requesting an expansion for runs sealed under an older budget.
        """
        _, data = self._restore(run_id)
        return None if data is None else StrategyBudget(**data["budget"])

    def observed_decisions(self, run_id: str) -> tuple[StrategyDecision, ...]:
        """Read decisions in attempt order from the authenticated checkpoint."""
        _, data = self._restore(run_id)
        if data is None:
            return ()
        return tuple(self._decision(data["decisions"][entry["attempt_id"]])
                     for entry in data["attempts"])

    @staticmethod
    def _pending_from(checkpoint: RuntimeCheckpoint | None) -> dict | None:
        if checkpoint is None:
            return None
        raw = checkpoint.retry_state.get(_PENDING_KEY)
        if raw is None:
            return None
        try:
            value = json.loads(canonical_json({"pending": raw}))["pending"]
            if (set(value) != {"schema", "mode", "attempt_id", "objective_digest",
                               "action", "usage", "budget"}
                    or value["schema"] != 1 or value["mode"] != "active_canary"):
                raise StrategyError("unsupported strategy reservation")
            if (not isinstance(value["attempt_id"], str)
                    or not 0 < len(value["attempt_id"]) <= 128):
                raise StrategyError("invalid reserved attempt identity")
            _digest(value["objective_digest"])
            StrategyAction(value["action"])
            usage = StrategyUsage(**value["usage"])
            StrategyBudget(**value["budget"])
            if usage.attempts != 1:
                raise StrategyError("reservation must charge exactly one attempt")
            return value
        except (TypeError, ValueError, KeyError) as exc:
            raise CheckpointError("invalid strategy reservation") from exc

    def pending(self, run_id: str) -> dict | None:
        """Return content-free sealed pre-action liability, if unresolved."""
        checkpoint, _ = self._restore(run_id)
        return self._pending_from(checkpoint)

    @staticmethod
    def _scope_guard_from(checkpoint: RuntimeCheckpoint | None) -> dict | None:
        if checkpoint is None:
            return None
        raw = checkpoint.retry_state.get(_SCOPE_GUARD_KEY)
        if raw is None:
            return None
        try:
            value = json.loads(canonical_json({"guard": raw}))["guard"]
            if (set(value) != {"schema", "mode", "owner_run_id", "objective_digest",
                               "member_run_ids"}
                    or value["schema"] != 1 or value["mode"] != "active_canary"
                    or not isinstance(value["owner_run_id"], str)
                    or not 0 < len(value["owner_run_id"]) <= 128
                    or not isinstance(value["member_run_ids"], list)
                    or not 0 < len(value["member_run_ids"]) <= 64
                    or any(not isinstance(item, str) or not 0 < len(item) <= 128
                           for item in value["member_run_ids"])
                    or len(set(value["member_run_ids"])) != len(value["member_run_ids"])):
                raise StrategyError("invalid strategy scope guard")
            _digest(value["objective_digest"])
            return value
        except (TypeError, ValueError, KeyError) as error:
            raise CheckpointError("invalid strategy scope guard") from error

    def scope_guard(self, scope_run_id: str) -> dict | None:
        """Read a sealed project-wide pending effect intent."""
        checkpoint, _ = self._restore(scope_run_id)
        return self._scope_guard_from(checkpoint)

    def acquire_scope_guard(self, scope_run_id: str, owner_run_id: str,
                            *, objective_digest: str,
                            member_run_ids: tuple[str, ...]) -> None:
        """Hold a project before its first build; any crash leaves it blocked."""
        if (not isinstance(owner_run_id, str)
                or not 0 < len(owner_run_id) <= 128):
            raise StrategyError("bounded scope guard owner required")
        if (type(member_run_ids) is not tuple or not 0 < len(member_run_ids) <= 64
                or len(set(member_run_ids)) != len(member_run_ids)
                or any(not isinstance(item, str) or not 0 < len(item) <= 128
                       for item in member_run_ids)):
            raise StrategyError("bounded scope guard members required")
        _digest(objective_digest)
        checkpoint, data = self._restore(scope_run_id)
        if data is not None or self._scope_guard_from(checkpoint) is not None:
            raise CheckpointError("unresolved strategy project guard blocks dispatch")
        expected = -1 if checkpoint is None else checkpoint.generation
        if checkpoint is None:
            checkpoint = RuntimeCheckpoint(scope_run_id, 0, {"strategy_schema": 1})
        guard = {
            "schema": 1, "mode": "active_canary", "owner_run_id": owner_run_id,
            "objective_digest": objective_digest,
            "member_run_ids": list(member_run_ids),
        }
        sealed = replace(
            checkpoint, generation=expected + 1,
            retry_state={**checkpoint.retry_state, _SCOPE_GUARD_KEY: guard},
        )
        self._repository.save(sealed, expected_generation=expected)

    def release_scope_guard(self, scope_run_id: str, owner_run_id: str,
                            *, member_run_ids: tuple[str, ...]) -> None:
        """Release only the exact owner after every member has settled."""
        checkpoint, _ = self._restore(scope_run_id)
        guard = self._scope_guard_from(checkpoint)
        if (guard is None or guard["owner_run_id"] != owner_run_id
                or tuple(guard["member_run_ids"]) != member_run_ids):
            raise CheckpointError("strategy project guard owner mismatch")
        for run_id in member_run_ids:
            member, data = self._restore(run_id)
            if self._pending_from(member) is not None:
                raise CheckpointError("unresolved strategy reservation blocks project release")
            if data is not None and data["attempts"]:
                last_id = data["attempts"][-1]["attempt_id"]
                last_decision = self._decision(data["decisions"][last_id])
                if last_decision.action is StrategyAction.RECONCILE:
                    raise CheckpointError("uncertain strategy effect blocks project release")
        sealed = replace(
            checkpoint, generation=checkpoint.generation + 1,
            retry_state={key: value for key, value in checkpoint.retry_state.items()
                         if key != _SCOPE_GUARD_KEY},
        )
        self._repository.save(sealed, expected_generation=checkpoint.generation)

    def reserve_next(self, run_id: str, attempt_id: str, *, objective_digest: str,
                     action: StrategyAction, usage: StrategyUsage,
                     budget: StrategyBudget) -> None:
        """Seal a charge before a paid canary action; never replay it blindly."""
        if (not isinstance(attempt_id, str) or not 0 < len(attempt_id) <= 128
                or not isinstance(action, StrategyAction)
                or not isinstance(usage, StrategyUsage)
                or not isinstance(budget, StrategyBudget)):
            raise StrategyError("typed bounded strategy reservation required")
        _digest(objective_digest)
        if usage.attempts != 1:
            raise StrategyError("reservation must charge exactly one attempt")
        checkpoint, data = self._restore(run_id)
        if self._pending_from(checkpoint) is not None:
            raise CheckpointError("unresolved strategy reservation blocks dispatch")
        history = () if data is None else tuple(StrategyAttempt.from_dict(x) for x in data["attempts"])
        if any(item.attempt_id == attempt_id for item in history):
            raise CheckpointError("strategy attempt already completed")
        if history and history[-1].signature.objective_digest != objective_digest:
            raise StrategyError("reservation objective changed")
        if data is not None and not StrategyBudget(**data["budget"]).allows(budget):
            raise StrategyError("persisted strategy budget cannot expand")
        consumed = StrategyUsage()
        for item in history:
            consumed = consumed.plus(item.usage)
        if len(history) >= 64 or not budget.allows(consumed.plus(usage)):
            raise StrategyError("strategy reservation exceeds remaining budget")
        pending = {
            "schema": 1, "mode": "active_canary", "attempt_id": attempt_id,
            "objective_digest": objective_digest, "action": action.value,
            "usage": asdict(usage), "budget": asdict(budget),
        }
        expected = -1 if checkpoint is None else checkpoint.generation
        if checkpoint is None:
            checkpoint = RuntimeCheckpoint(run_id, 0, {"strategy_schema": 1})
        sealed = replace(
            checkpoint, generation=expected + 1,
            retry_state={**checkpoint.retry_state, _PENDING_KEY: pending},
        )
        self._repository.save(sealed, expected_generation=expected)

    def record(self, attempt: StrategyAttempt, *, budget: StrategyBudget,
               available_actions: tuple[StrategyAction, ...], unresolved_effects: bool = False,
               policy_blocked: bool = False, artifacts_ready: bool = True,
               transport_replay_safe: bool = False,
               expected_prior: tuple[str, ...] | None = None) -> StrategyDecision:
        """Append or replay one attempt in a single generation CAS.

        ``expected_prior`` names the sealed attempts, in order, that precede
        this attempt in the history its usage was derived from. When given,
        the restored history must match it exactly or ``CheckpointConflict``
        is raised before anything is written, so a charge computed from a
        stale read is never sealed.
        """
        return self._record(
            attempt, budget=budget, available_actions=available_actions,
            unresolved_effects=unresolved_effects, policy_blocked=policy_blocked,
            artifacts_ready=artifacts_ready, transport_replay_safe=transport_replay_safe,
            reserved=False, reserved_action=None, expected_prior=expected_prior,
        )

    def record_reserved(self, attempt: StrategyAttempt, *, budget: StrategyBudget,
                        action: StrategyAction,
                        available_actions: tuple[StrategyAction, ...],
                        unresolved_effects: bool = False,
                        policy_blocked: bool = False,
                        artifacts_ready: bool = True,
                        transport_replay_safe: bool = False) -> StrategyDecision:
        """Close the exact sealed liability and append its result in one CAS."""
        return self._record(
            attempt, budget=budget, available_actions=available_actions,
            unresolved_effects=unresolved_effects, policy_blocked=policy_blocked,
            artifacts_ready=artifacts_ready, transport_replay_safe=transport_replay_safe,
            reserved=True, reserved_action=action,
        )

    def _record(self, attempt: StrategyAttempt, *, budget: StrategyBudget,
                available_actions: tuple[StrategyAction, ...], unresolved_effects: bool,
                policy_blocked: bool, artifacts_ready: bool,
                transport_replay_safe: bool, reserved: bool,
                reserved_action: StrategyAction | None,
                expected_prior: tuple[str, ...] | None = None) -> StrategyDecision:
        if not isinstance(attempt, StrategyAttempt) or not isinstance(budget, StrategyBudget):
            raise StrategyError("typed strategy attempt and budget required")
        if attempt.usage.attempts != 1:
            raise StrategyError("each durable strategy attempt must charge exactly one attempt")
        checkpoint, data = self._restore(attempt.run_id)
        pending = self._pending_from(checkpoint)
        if pending is not None:
            if not reserved:
                raise CheckpointError("unresolved strategy reservation blocks observation")
            if (pending["attempt_id"] != attempt.attempt_id
                    or pending["objective_digest"] != attempt.signature.objective_digest):
                raise CheckpointError("strategy reservation identity mismatch")
            if (not isinstance(reserved_action, StrategyAction)
                    or pending["action"] != reserved_action.value):
                raise CheckpointError("strategy reservation action mismatch")
            if not StrategyBudget(**pending["budget"]).allows(budget):
                raise StrategyError("strategy reservation budget cannot expand")
            if StrategyUsage(**pending["usage"]) != attempt.usage:
                raise StrategyError("completion must retain reservation usage without refund")
        elif reserved:
            if data is None or not any(
                    StrategyAttempt.from_dict(x) == attempt for x in data["attempts"]):
                raise CheckpointError("strategy reservation is absent")
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
        if expected_prior is not None:
            prior = []
            for old in history:
                if old.attempt_id == attempt.attempt_id:
                    break
                prior.append(old.attempt_id)
            if tuple(prior) != tuple(expected_prior):
                raise CheckpointConflict("strategy history changed after the attempt was derived")
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
            if pending is not None:
                raise CheckpointError("completed attempt cannot hold another reservation")
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
                             decisions={**checkpoint.decisions, "strategy_v1": strategy},
                             retry_state={key: value for key, value in checkpoint.retry_state.items()
                                          if key != _PENDING_KEY} if reserved else checkpoint.retry_state)
        self._repository.save(checkpoint, expected_generation=expected)
        return decision
