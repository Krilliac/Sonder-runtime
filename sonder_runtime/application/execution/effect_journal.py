"""Durable intent/outcome contract for mutating worker effects.

The journal is deliberately independent of a particular tool or runner.  A
worker binds a journal while it owns a run; the typed tool gateway then records
the intent immediately before invoking a mutating tool and records the outcome
only after the invoker returns.  An intent without an outcome is never treated
as success during restart recovery.
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator, Mapping, Protocol


class EffectJournalError(ValueError):
    """The journal rejected an invalid or conflicting transition."""


class EffectState(str, Enum):
    INTENT = "intent"
    COMPLETED = "completed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class EffectIntent:
    intent_id: str
    run_id: str
    worker_id: str
    operation_id: str
    scope: str
    owner_epoch: int
    idempotency_key: str
    request_digest: str
    reconciliation: str = "manual"
    sequence: int = 0
    state: EffectState = EffectState.INTENT
    outcome_digest: str = ""
    receipt_key: str = ""
    detail: str = ""
    replayed: bool = False

    def __post_init__(self) -> None:
        for name in ("intent_id", "run_id", "worker_id", "operation_id", "scope",
                     "idempotency_key", "request_digest"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise EffectJournalError(f"{name} must be non-empty")
        if type(self.owner_epoch) is not int or self.owner_epoch < 1:
            raise EffectJournalError("owner_epoch must be positive")
        if self.reconciliation not in {"idempotent", "query", "manual"}:
            raise EffectJournalError("unsupported reconciliation strategy")
        if type(self.sequence) is not int or self.sequence < 0:
            raise EffectJournalError("sequence cannot be negative")
        if not isinstance(self.replayed, bool):
            raise EffectJournalError("replayed must be boolean")
        if self.state in {EffectState.COMPLETED, EffectState.FAILED}:
            if not self.outcome_digest or not self.receipt_key:
                raise EffectJournalError("terminal outcome requires digest and receipt")


@dataclass(frozen=True, slots=True)
class EffectOutcome:
    intent_id: str
    state: EffectState
    outcome_digest: str
    receipt_key: str
    detail: str = ""
    worker_id: str = ""
    owner_epoch: int | None = None

    def __post_init__(self) -> None:
        if self.state not in {EffectState.COMPLETED, EffectState.FAILED, EffectState.UNCERTAIN}:
            raise EffectJournalError("outcome must be terminal or uncertain")
        if not self.intent_id.strip() or not self.outcome_digest.strip():
            raise EffectJournalError("outcome identity and digest are required")
        if self.state is not EffectState.UNCERTAIN and not self.receipt_key.strip():
            raise EffectJournalError("definitive outcome requires receipt")
        if (
            not isinstance(self.worker_id, str) or not self.worker_id.strip()
            or type(self.owner_epoch) is not int or self.owner_epoch < 1
        ):
            raise EffectJournalError("definitive outcome requires worker identity and epoch")


@dataclass(frozen=True, slots=True)
class ReconciliationProof:
    """Host-verifier result bound to the exact external effect identity.

    The journal accepts this value only from a verifier registered by trusted
    host composition.  Free-form caller text is deliberately absent: the
    verifier must return the operation, receipt, and outcome digest it
    obtained from the external system.
    """

    intent_id: str
    operation_id: str
    receipt_key: str
    outcome_digest: str
    state: EffectState
    verifier_id: str
    external_reference: str

    def __post_init__(self) -> None:
        if self.state not in {EffectState.COMPLETED, EffectState.FAILED}:
            raise EffectJournalError("reconciliation proof must be definitive")
        for name in (
            "intent_id", "operation_id", "receipt_key", "outcome_digest",
            "verifier_id", "external_reference",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise EffectJournalError(f"reconciliation proof {name} is required")


class EffectReconciliationVerifier(Protocol):
    """Host-owned verifier for one explicitly supported operation family."""

    verifier_id: str
    operation_ids: frozenset[str]

    def verify(self, intent: EffectIntent) -> ReconciliationProof | None: ...


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    run_id: str
    action: str
    intent_ids: tuple[str, ...] = ()
    high_water: int = 0
    detail: str = ""


@dataclass(frozen=True, slots=True)
class EffectJournalPage:
    """Read-only, bounded view of one run's journal after a checkpoint position.

    ``records`` holds the intents with ``sequence > after_sequence`` in
    sequence order (optionally only one worker's), at most ``limit`` of them.
    ``high_water`` (the run's maximum sequence) and ``settled_high_water``
    (see ``EffectJournalReader.settled_high_water``) are read in the same
    snapshot as ``records`` and always cover the whole run, not the page.
    ``truncated`` is true when more matching records exist; the caller must
    page with ``after_sequence=records[-1].sequence`` before concluding
    anything about the remainder.
    """

    run_id: str
    after_sequence: int
    records: tuple[EffectIntent, ...]
    high_water: int
    settled_high_water: int
    truncated: bool

    @property
    def unresolved(self) -> tuple[EffectIntent, ...]:
        """Records in this page with no definitive outcome (intent/uncertain)."""
        return tuple(
            record for record in self.records
            if record.state in {EffectState.INTENT, EffectState.UNCERTAIN}
        )


class EffectJournalReader(Protocol):
    """Read-only queries for binding a checkpoint to a journal position.

    Neither method writes, claims ownership, or changes a recovery fence.
    """

    def settled_high_water(self, run_id: str) -> int: ...
    def effects_since(
        self, run_id: str, after_sequence: int, *, limit: int = 100,
        worker_id: str | None = None,
    ) -> EffectJournalPage: ...


class EffectJournal(Protocol):
    def begin(self, intent: EffectIntent) -> EffectIntent: ...
    def outcome(self, outcome: EffectOutcome) -> EffectIntent: ...
    def uncertain(self, intent_id: str, *, detail: str) -> EffectIntent: ...
    def high_water(self, run_id: str) -> int: ...
    def recover(self, run_id: str, *, live_workers: Mapping[str, int], max_records: int = 100) -> RecoveryDecision: ...
    def validate_checkpoint(self, run_id: str, high_water: int) -> None: ...
    def append_checkpoint(self, run_id: str, state_digest: str) -> Mapping[str, object]: ...
    def restore_checkpoint(self, run_id: str) -> Mapping[str, object] | None: ...
    def claim_owner(self, run_id: str, worker_id: str, owner_epoch: int) -> None: ...
    def outcome_and_checkpoint(self, outcome: EffectOutcome, state: object) -> Mapping[str, object]: ...
    def reconcile(self, intent_id: str, *, owner_epoch: int) -> EffectIntent: ...


@dataclass
class JournalBinding:
    journal: EffectJournal
    run_id: str
    worker_id: str
    owner_epoch: int
    scope: str

    def begin_request(self, *, operation_id: str, idempotency_key: str,
                      request_digest: str, reconciliation: str = "manual") -> EffectIntent:
        intent = EffectIntent(
            f"{self.run_id}:{operation_id}", self.run_id, self.worker_id,
            operation_id, self.scope, self.owner_epoch, idempotency_key,
            request_digest, reconciliation,
        )
        existing = getattr(self.journal, "get", lambda _intent_id: None)(intent.intent_id)
        stored = self.journal.begin(intent)
        if existing is not None or stored.replayed:
            raise EffectJournalError(
                "duplicate effect intent requires reconciliation before invocation"
            )
        return stored

    def complete(self, intent: EffectIntent, *, outcome_digest: str,
                 receipt_key: str, detail: str = "", success: bool = True) -> EffectIntent:
        return self.journal.outcome(EffectOutcome(
            intent.intent_id,
            EffectState.COMPLETED if success else EffectState.FAILED,
            outcome_digest, receipt_key, detail,
            self.worker_id, self.owner_epoch,
        ))

    def mark_uncertain(self, intent: EffectIntent, *, detail: str) -> EffectIntent:
        return self.journal.uncertain(intent.intent_id, detail=detail)


_CURRENT: contextvars.ContextVar[JournalBinding | None] = contextvars.ContextVar(
    "sonder_effect_journal", default=None,
)


def current() -> JournalBinding | None:
    return _CURRENT.get()


@contextlib.contextmanager
def bound(binding: JournalBinding) -> Iterator[JournalBinding]:
    if not isinstance(binding, JournalBinding):
        raise TypeError("binding must be a JournalBinding")
    token = _CURRENT.set(binding)
    try:
        yield binding
    finally:
        _CURRENT.reset(token)


__all__ = ["EffectIntent", "EffectJournal", "EffectJournalError", "EffectJournalPage",
           "EffectJournalReader", "EffectOutcome",
           "EffectReconciliationVerifier", "EffectState", "JournalBinding",
           "ReconciliationProof", "RecoveryDecision", "bound", "current"]
