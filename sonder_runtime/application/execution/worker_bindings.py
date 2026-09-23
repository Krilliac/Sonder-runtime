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
from dataclasses import dataclass
from typing import Any, Callable, Mapping, TypeVar

from .effect_journal import (
    EffectJournal,
    EffectJournalError,
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
    key = receipt_key(result) if callable(receipt_key) else receipt_key
    if not isinstance(key, str) or not key.strip():
        binding.mark_uncertain(intent, detail="worker returned no durable receipt key")
        raise EffectJournalError("worker mutation returned no durable receipt key")
    is_success = success(result) if callable(success) else success
    binding.complete(
        intent,
        outcome_digest=_digest(result),
        receipt_key=key,
        success=bool(is_success),
    )
    append_checkpoint = getattr(context.journal, "append_checkpoint", None)
    if callable(append_checkpoint):
        # The result is already sealed by the terminal journal outcome.  The
        # worker checkpoint records that exact state and the journal's current
        # high-water in one host-owned transaction.
        append_checkpoint(context.run_id, _digest(result))
    return result


__all__ = ["AuthenticatedWorkerBinding", "journaled_effect"]
