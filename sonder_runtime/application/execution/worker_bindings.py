"""Authenticated bindings for mutating worker-owned effects.

The typed tool gateway already records effects while a binding is active.  A
few worker families perform their durable mutation directly (process launch,
compute dispatch, child execution, and selfmod activation), so they use this
small adapter to apply the same intent/outcome protocol at their own public
boundary.  The context is supplied by trusted composition code; callers do
not get to choose a worker identity or owner epoch per operation.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

from .effect_journal import (
    EffectJournal,
    EffectJournalError,
    EffectOutcome,
    EffectState,
    JournalBinding,
    RecoveryDecision,
)


T = TypeVar("T")


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    ).hexdigest()


def _json_safe(value: Any) -> Any:
    """Convert a bounded worker receipt/state into deterministic JSON data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    raise EffectJournalError(
        f"worker checkpoint state is not serializable: {type(value).__name__}"
    )


@dataclass(frozen=True, slots=True)
class AuthenticatedWorkerBinding:
    """Host-authenticated identity for one worker run."""

    journal: EffectJournal
    run_id: str
    worker_id: str
    owner_epoch: int
    scope: str

    def __post_init__(self) -> None:
        for name in ("run_id", "worker_id", "scope"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if type(self.owner_epoch) is not int or self.owner_epoch < 1:
            raise ValueError("owner_epoch must be positive")
        for name in ("begin", "outcome", "uncertain", "recover"):
            if not callable(getattr(self.journal, name, None)):
                raise TypeError("journal does not implement the effect contract")

    def binding(self) -> JournalBinding:
        return JournalBinding(
            self.journal, self.run_id, self.worker_id, self.owner_epoch, self.scope,
        )

    def recover_before_restart(
        self,
        *,
        live_workers: Mapping[str, int] | None = None,
        max_records: int = 100,
    ) -> RecoveryDecision:
        """Refuse worker restart while an old owner needs reconciliation."""
        claim_owner = getattr(self.journal, "claim_owner", None)
        if callable(claim_owner):
            # Advance the durable fence before inspecting old effects.  Even
            # when recovery raises, an older binding can no longer admit work.
            claim_owner(self.run_id, self.worker_id, self.owner_epoch)
        decision = self.journal.recover(
            self.run_id,
            # A constructed binding is not proof that an old worker is still
            # alive.  Composition may pass an independently authenticated
            # liveness map, but the safe default treats every prior owner as
            # unavailable and requires reconciliation before restart.
            live_workers={} if live_workers is None else live_workers,
            max_records=max_records,
        )
        if decision.action == "reconcile":
            raise EffectJournalError(
                "worker restart requires explicit reconciliation of uncertain effects"
            )
        restore_checkpoint = getattr(self.journal, "restore_checkpoint", None)
        if callable(restore_checkpoint):
            try:
                restore_checkpoint(self.run_id)
            except EffectJournalError as exc:
                raise EffectJournalError(
                    "worker restart requires checkpoint reconciliation"
                ) from exc
        return decision


def journaled_effect(
    context: AuthenticatedWorkerBinding,
    *,
    operation_id: str,
    idempotency_key: str,
    request: Any,
    invoke: Callable[[], T],
    receipt_key: Callable[[T], str] | str,
    reconciliation: str = "manual",
    success: Callable[[T], bool] | bool = True,
    checkpoint_state: Callable[[T], Any] | Any | None = None,
) -> T:
    """Record one direct worker mutation around its real invocation."""
    binding = context.binding()
    intent = binding.begin_request(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        request_digest=_digest(request),
        reconciliation=reconciliation,
    )
    try:
        result = invoke()
    except BaseException as exc:
        binding.mark_uncertain(intent, detail=f"worker raised {type(exc).__name__}")
        raise
    try:
        _publish_outcome(
            context, binding, intent, result,
            receipt_key=receipt_key, success=success,
            checkpoint_state=checkpoint_state,
        )
    except BaseException as exc:
        # The external effect has already run.  If its receipt or checkpoint
        # cannot be published, the admitted intent must not remain a bare
        # INTENT: recovery may reattach a bare intent to a live owner, which
        # could invoke the effect a second time.  Mark it uncertain so only
        # explicit reconciliation can resolve it.  If the journal cannot record
        # that either, the intent stays unresolved and restart recovery still
        # treats it as orphaned; the original failure is what the caller sees.
        try:
            binding.mark_uncertain(
                intent,
                # Type name only: exception text may quote worker output.
                detail=f"effect ran but receipt publication failed: {type(exc).__name__}",
            )
        except Exception:
            pass
        raise
    return result


def _publish_outcome(
    context: AuthenticatedWorkerBinding,
    binding: JournalBinding,
    intent: Any,
    result: Any,
    *,
    receipt_key: Callable[[Any], str] | str,
    success: Callable[[Any], bool] | bool,
    checkpoint_state: Callable[[Any], Any] | Any | None,
) -> None:
    """Publish the receipt and checkpoint of an effect that already ran."""
    key = receipt_key(result) if callable(receipt_key) else receipt_key
    if not isinstance(key, str) or not key.strip():
        raise EffectJournalError("worker mutation returned no durable receipt key")
    is_success = success(result) if callable(success) else success
    outcome = EffectOutcome(
        intent.intent_id,
        EffectState.COMPLETED if is_success else EffectState.FAILED,
        _digest(result), key, worker_id=context.worker_id,
        owner_epoch=context.owner_epoch,
    )
    state = checkpoint_state(result) if callable(checkpoint_state) else checkpoint_state
    if state is None:
        state = {
            "effect": {
                "intent_id": intent.intent_id,
                "operation_id": intent.operation_id,
                "receipt_key": key,
                "outcome_digest": outcome.outcome_digest,
            },
        }
    else:
        state = _json_safe(state)
    outcome_and_checkpoint = getattr(context.journal, "outcome_and_checkpoint", None)
    if callable(outcome_and_checkpoint):
        # SQLiteEffectJournal commits both records under one BEGIN IMMEDIATE.
        outcome_and_checkpoint(outcome, state)
    else:
        binding.complete(
            intent,
            outcome_digest=outcome.outcome_digest,
            receipt_key=key,
            success=bool(is_success),
        )
        append_checkpoint = getattr(context.journal, "append_checkpoint", None)
        if callable(append_checkpoint):
            append_checkpoint(
                context.run_id, state,
                worker_id=context.worker_id, owner_epoch=context.owner_epoch,
            )


__all__ = ["AuthenticatedWorkerBinding", "journaled_effect"]
