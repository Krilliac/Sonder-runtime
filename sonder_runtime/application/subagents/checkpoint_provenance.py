"""Journal provenance for child checkpoints and the resume decision (#515).

A child checkpoint is only a safe continuation point when the host can show
which effects it already relied on.  This module provides both halves:

* ``JournalProvenanceStamp`` is the host hook installed on
  ``DurableContinuationService``.  It reads the journal first (identity,
  current owner epoch and settled high-water in one snapshot) and stamps an
  immutable ``CheckpointProvenance`` that the child store then persists in the
  same transaction as its compare-and-set.  Runner code never supplies it.
* ``validate_checkpoint_resume`` is a pure decision over a checkpoint and a
  bounded ``effects_since`` page reader.  It never executes work, claims an
  owner or writes the journal.  It fails closed with a typed reason for
  missing or swapped journals, identity mismatches, stale epochs, unsettled
  or overlapping intents, incomplete pages and tampered state.

The journal and the child store are separate SQLite files, so there is no
cross-store atomicity.  The protocol is journal-proof-first: an effect's
receipt commits before the host stamps the position it may rely on, and the
child compare-and-set commits afterwards.  A crash between the two leaves the
previous checkpoint and its older, still-valid provenance.  This module does
not itself resume anything: ``LocalSubagentProvider.resume`` (composed in
``bootstrap/app.py``) calls these validators before claiming a child.
"""
from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol

from ..execution.effect_journal import (
    EffectIntent,
    EffectJournalError,
    EffectJournalPage,
    EffectState,
)
from ..ports.subagents import InvalidSubagentRequest
from .continuable import (
    CheckpointProvenance,
    ContinuableCheckpoint,
    checkpoint_state_digest,
)

DEFAULT_PAGE_LIMIT = 100
DEFAULT_MAX_PAGES = 100
_MAX_PAGE_LIMIT = 10_000
_MAX_PAGES = 10_000
_UNRESOLVED = frozenset({EffectState.INTENT, EffectState.UNCERTAIN})


class CheckpointProvenanceError(RuntimeError):
    """The host could not stamp provenance; the checkpoint must not be saved."""


@dataclass(frozen=True, slots=True)
class ProvenanceSubject:
    """What a checkpoint about to be written claims about itself."""

    child_id: str
    sequence: int
    state_digest: str
    cursor: str | None


@dataclass(frozen=True, slots=True)
class ProvenanceBinding:
    """Host-authenticated journal owner for one child run."""

    run_id: str
    worker_id: str
    owner_epoch: int

    def __post_init__(self) -> None:
        for name in ("run_id", "worker_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise CheckpointProvenanceError(f"provenance {name} is required")
        if type(self.owner_epoch) is not int or self.owner_epoch < 1:
            raise CheckpointProvenanceError("provenance owner epoch must be positive")


@dataclass(frozen=True, slots=True)
class JournalPosition:
    """One read snapshot of a journal's identity and a run's position."""

    journal_identity: str
    run_id: str
    worker_id: str
    current_owner_epoch: int | None
    settled_high_water: int
    high_water: int


class ProvenanceJournal(Protocol):
    """Read-only journal view with a durable identity.

    ``position`` returns ``None`` when the journal (or its identity) is
    missing.  Implementations raise ``EffectJournalError`` for storage
    failures; they never create an identity while being read for validation.
    """

    def position(self, run_id: str, worker_id: str) -> JournalPosition | None: ...
    def settled_high_water(self, run_id: str) -> int: ...
    def effects_since(
        self, run_id: str, after_sequence: int, *, limit: int = 100,
        worker_id: str | None = None,
    ) -> EffectJournalPage: ...


CheckpointProvenanceHook = Callable[[ProvenanceSubject], CheckpointProvenance]


class JournalProvenanceStamp:
    """Host hook: stamp a checkpoint with the journal position it relies on."""

    def __init__(self, journal: ProvenanceJournal,
                 resolve: Callable[[ProvenanceSubject], ProvenanceBinding]) -> None:
        if not callable(getattr(journal, "position", None)):
            raise TypeError("journal does not expose a provenance position")
        if not callable(resolve):
            raise TypeError("provenance binding resolver must be callable")
        self._journal, self._resolve = journal, resolve

    def __call__(self, subject: ProvenanceSubject) -> CheckpointProvenance:
        if not isinstance(subject, ProvenanceSubject):
            raise CheckpointProvenanceError("provenance subject is required")
        binding = self._resolve(subject)
        if not isinstance(binding, ProvenanceBinding):
            raise CheckpointProvenanceError("provenance resolver returned no host binding")
        position = self._journal.position(binding.run_id, binding.worker_id)
        if position is None:
            raise CheckpointProvenanceError("effect journal identity is unavailable")
        if position.current_owner_epoch != binding.owner_epoch:
            # A superseded (or never-claimed) owner cannot vouch for the
            # journal; the child keeps its previous checkpoint.
            raise CheckpointProvenanceError("checkpoint writer is not the current journal owner")
        try:
            return CheckpointProvenance.stamp(
                child_id=subject.child_id, sequence=subject.sequence,
                state_digest=subject.state_digest, cursor=subject.cursor,
                journal_identity=position.journal_identity, run_id=binding.run_id,
                worker_id=binding.worker_id, owner_epoch=binding.owner_epoch,
                settled_position=position.settled_high_water,
            )
        except InvalidSubagentRequest as exc:
            raise CheckpointProvenanceError(str(exc)) from exc


class CheckpointResumeRefusal(str, Enum):
    NO_CHECKPOINT = "no_checkpoint"
    PROVENANCE_ABSENT = "provenance_absent"
    PROVENANCE_DIGEST_MISMATCH = "provenance_digest_mismatch"
    SUBJECT_MISMATCH = "subject_mismatch"
    STATE_DIGEST_MISMATCH = "state_digest_mismatch"
    JOURNAL_MISSING = "journal_missing"
    JOURNAL_UNAVAILABLE = "journal_unavailable"
    JOURNAL_IDENTITY_MISMATCH = "journal_identity_mismatch"
    RUN_MISMATCH = "run_mismatch"
    WORKER_MISMATCH = "worker_mismatch"
    STALE_OWNER_EPOCH = "stale_owner_epoch"
    OWNER_EPOCH_AHEAD = "owner_epoch_ahead"
    OWNER_SUPERSEDED = "owner_superseded"
    POSITION_AHEAD_OF_JOURNAL = "position_ahead_of_journal"
    UNRESOLVED_AT_OR_BELOW_POSITION = "unresolved_at_or_below_position"
    OVERLAPPING_UNRESOLVED_INTENT = "overlapping_unresolved_intent"
    UNRESOLVED_AFTER_POSITION = "unresolved_after_position"
    INCOMPLETE_JOURNAL_PAGE = "incomplete_journal_page"
    JOURNAL_PAGE_BUDGET_EXHAUSTED = "journal_page_budget_exhausted"
    JOURNAL_CHANGED = "journal_changed_during_validation"


@dataclass(frozen=True, slots=True)
class SettledReceipt:
    """A definitive journal outcome a resumed runner must consume, not repeat."""

    idempotency_key: str
    sequence: int
    intent_id: str
    operation_id: str
    worker_id: str
    owner_epoch: int
    state: EffectState
    receipt_key: str
    outcome_digest: str


def _frozen(value: Mapping[str, SettledReceipt] | None = None) -> Mapping[str, SettledReceipt]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class CheckpointResumeDecision:
    """Whether a checkpoint may be used as a resume point, and on what receipts.

    ``receipts`` are the settled outcomes at or below the checkpoint's
    position, keyed by idempotency key in sequence order.  ``later_receipts``
    are settled outcomes after it.  A runner resuming from ``resume_state``
    must consume both instead of re-invoking those effects.
    """

    allowed: bool
    reason: CheckpointResumeRefusal | None
    detail: str
    child_id: str | None = None
    sequence: int | None = None
    settled_position: int | None = None
    resume_state: Mapping[str, Any] | None = None
    receipts: Mapping[str, SettledReceipt] = field(default_factory=_frozen)
    later_receipts: Mapping[str, SettledReceipt] = field(default_factory=_frozen)


def _refuse(reason: CheckpointResumeRefusal, detail: str,
            checkpoint: ContinuableCheckpoint | None = None) -> CheckpointResumeDecision:
    return CheckpointResumeDecision(
        False, reason, detail,
        child_id=None if checkpoint is None else checkpoint.child_id,
        sequence=None if checkpoint is None else checkpoint.sequence,
    )


def _receipt(record: EffectIntent) -> SettledReceipt:
    return SettledReceipt(
        record.idempotency_key, record.sequence, record.intent_id,
        record.operation_id, record.worker_id, record.owner_epoch, record.state,
        record.receipt_key, record.outcome_digest,
    )


def _read_run(journal: ProvenanceJournal, run_id: str, *, page_limit: int,
              max_pages: int) -> tuple[tuple[EffectIntent, ...], EffectJournalPage] | CheckpointResumeDecision:
    """Read every intent of ``run_id`` through complete, contiguous pages."""
    records: list[EffectIntent] = []
    after = 0
    for _ in range(max_pages):
        page = journal.effects_since(run_id, after, limit=page_limit)
        if not isinstance(page, EffectJournalPage) or page.run_id != run_id \
                or page.after_sequence != after or len(page.records) > page_limit:
            return _refuse(CheckpointResumeRefusal.INCOMPLETE_JOURNAL_PAGE,
                           "journal page does not answer the requested range")
        for record in page.records:
            if not isinstance(record, EffectIntent) or record.run_id != run_id:
                return _refuse(CheckpointResumeRefusal.INCOMPLETE_JOURNAL_PAGE,
                               "journal page contains a foreign record")
            if record.sequence != after + 1:
                return _refuse(CheckpointResumeRefusal.INCOMPLETE_JOURNAL_PAGE,
                               f"journal sequence {after + 1} is missing")
            records.append(record)
            after = record.sequence
        if page.truncated:
            if not page.records:
                return _refuse(CheckpointResumeRefusal.INCOMPLETE_JOURNAL_PAGE,
                               "truncated journal page returned no records")
            continue
        if after != page.high_water:
            return _refuse(CheckpointResumeRefusal.INCOMPLETE_JOURNAL_PAGE,
                           "journal page ended before the run's high-water")
        return tuple(records), page
    return _refuse(CheckpointResumeRefusal.JOURNAL_PAGE_BUDGET_EXHAUSTED,
                   "journal run exceeds the validation page budget")


def validate_checkpoint_resume(
    checkpoint: ContinuableCheckpoint | None,
    journal: ProvenanceJournal | None,
    *,
    run_id: str,
    worker_id: str,
    resumer_owner_epoch: int,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> CheckpointResumeDecision:
    """Decide whether ``checkpoint`` may seed a resumed child run.

    ``run_id``/``worker_id`` are the host's expected journal owner for the
    child; ``resumer_owner_epoch`` is the epoch the resuming host has already
    claimed in the journal.  Allowed only when the provenance is intact and
    matches the checkpoint, the journal identity, run and worker match, the
    stamped epoch is current or has been reclaimed by the resumer, every
    effect at or below the stamped position is settled, nothing in the run is
    unresolved, and the whole run was read through complete pages.
    """
    for name, value in (("run_id", run_id), ("worker_id", worker_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} is required")
    if type(resumer_owner_epoch) is not int or resumer_owner_epoch < 1:
        raise ValueError("resumer_owner_epoch must be positive")
    if type(page_limit) is not int or not 1 <= page_limit <= _MAX_PAGE_LIMIT:
        raise ValueError("page_limit must be within 1..10000")
    if type(max_pages) is not int or not 1 <= max_pages <= _MAX_PAGES:
        raise ValueError("max_pages must be within 1..10000")
    R = CheckpointResumeRefusal
    if checkpoint is None:
        return _refuse(R.NO_CHECKPOINT, "child has no checkpoint")
    if not isinstance(checkpoint, ContinuableCheckpoint):
        raise TypeError("checkpoint must be a ContinuableCheckpoint")
    provenance = checkpoint.provenance
    if checkpoint.provenance_absent or provenance is None:
        return _refuse(R.PROVENANCE_ABSENT,
                       "checkpoint has no journal provenance; whole-child fencing applies",
                       checkpoint)
    if not provenance.digest_valid:
        return _refuse(R.PROVENANCE_DIGEST_MISMATCH, "provenance record digest is invalid", checkpoint)
    if (provenance.child_id, provenance.sequence, provenance.cursor) != (
            checkpoint.child_id, checkpoint.sequence, checkpoint.cursor):
        return _refuse(R.SUBJECT_MISMATCH, "provenance describes a different checkpoint", checkpoint)
    try:
        state_digest = checkpoint_state_digest(checkpoint.state)
    except InvalidSubagentRequest:
        state_digest = ""
    if state_digest != provenance.state_digest:
        return _refuse(R.STATE_DIGEST_MISMATCH, "checkpoint state does not match its provenance", checkpoint)
    if journal is None:
        return _refuse(R.JOURNAL_MISSING, "no effect journal is available", checkpoint)
    try:
        position = journal.position(run_id, worker_id)
        if position is None:
            return _refuse(R.JOURNAL_MISSING, "effect journal or its identity is missing", checkpoint)
        if position.journal_identity != provenance.journal_identity:
            return _refuse(R.JOURNAL_IDENTITY_MISMATCH,
                           "checkpoint was stamped against a different journal", checkpoint)
        if provenance.run_id != run_id:
            return _refuse(R.RUN_MISMATCH, "checkpoint belongs to a different journal run", checkpoint)
        if provenance.worker_id != worker_id:
            return _refuse(R.WORKER_MISMATCH, "checkpoint belongs to a different worker", checkpoint)
        current_epoch = position.current_owner_epoch
        if current_epoch is None or resumer_owner_epoch != current_epoch:
            return _refuse(R.STALE_OWNER_EPOCH,
                           "resumer does not hold the journal's current owner epoch", checkpoint)
        if provenance.owner_epoch > current_epoch:
            return _refuse(R.OWNER_EPOCH_AHEAD,
                           "checkpoint epoch is ahead of the journal owner", checkpoint)
        read = _read_run(journal, run_id, page_limit=page_limit, max_pages=max_pages)
        if not isinstance(read, CheckpointResumeDecision):
            # Pages are separate read snapshots.  Re-read the identity and
            # owner so a journal swapped or reclaimed while the pages were
            # read refuses instead of mixing two journals or two owners.
            confirm = journal.position(run_id, worker_id)
            if (confirm is None
                    or confirm.journal_identity != position.journal_identity
                    or confirm.current_owner_epoch != current_epoch):
                return _refuse(R.JOURNAL_CHANGED,
                               "effect journal changed while it was being read", checkpoint)
    except EffectJournalError as exc:
        return _refuse(R.JOURNAL_UNAVAILABLE, f"effect journal read failed: {exc}", checkpoint)
    if isinstance(read, CheckpointResumeDecision):
        return _refuse(read.reason, read.detail, checkpoint)  # type: ignore[arg-type]
    records, final_page = read
    stamped = provenance.settled_position
    if stamped > final_page.high_water:
        return _refuse(R.POSITION_AHEAD_OF_JOURNAL,
                       "checkpoint relies on journal positions that do not exist", checkpoint)
    covered, later = records[:stamped], records[stamped:]
    unresolved_below = [record for record in covered if record.state in _UNRESOLVED]
    if unresolved_below:
        return _refuse(R.UNRESOLVED_AT_OR_BELOW_POSITION,
                       f"journal sequence {unresolved_below[0].sequence} is not settled", checkpoint)
    for record in covered:
        if record.worker_id == provenance.worker_id and record.owner_epoch > provenance.owner_epoch:
            return _refuse(R.OWNER_SUPERSEDED,
                           "a newer owner settled effects the checkpoint claims", checkpoint)
    settled_keys: dict[str, SettledReceipt] = {}
    for record in covered:
        settled_keys[record.idempotency_key] = _receipt(record)
    seen = set(settled_keys)
    later_receipts: dict[str, SettledReceipt] = {}
    first_unresolved: EffectIntent | None = None
    for record in later:
        if record.idempotency_key in seen:
            if record.state in _UNRESOLVED:
                return _refuse(R.OVERLAPPING_UNRESOLVED_INTENT,
                               "an unresolved intent reuses a settled idempotency key", checkpoint)
            return _refuse(R.JOURNAL_CHANGED,
                           "journal repeats an idempotency key", checkpoint)
        seen.add(record.idempotency_key)
        if record.state in _UNRESOLVED:
            first_unresolved = first_unresolved or record
        else:
            later_receipts[record.idempotency_key] = _receipt(record)
    if first_unresolved is not None:
        return _refuse(R.UNRESOLVED_AFTER_POSITION,
                       f"journal sequence {first_unresolved.sequence} requires reconciliation",
                       checkpoint)
    if final_page.settled_high_water != final_page.high_water:
        return _refuse(R.JOURNAL_CHANGED,
                       "journal settled high-water moved during validation", checkpoint)
    return CheckpointResumeDecision(
        True, None, "checkpoint provenance matches a settled journal prefix",
        child_id=checkpoint.child_id, sequence=checkpoint.sequence,
        settled_position=stamped, resume_state=MappingProxyType(dict(checkpoint.state)),
        receipts=_frozen(settled_keys), later_receipts=_frozen(later_receipts),
    )


class ChildResumeRefused(InvalidSubagentRequest):
    """A child may not resume from its checkpoint; it stays recovery_required."""

    recovery_required = True

    def __init__(self, decision: CheckpointResumeDecision) -> None:
        reason = decision.reason.value if decision.reason is not None else "refused"
        super().__init__(f"child resume refused ({reason}): {decision.detail}")
        self.decision = decision
        self.reason = decision.reason


_RESUMED: contextvars.ContextVar[CheckpointResumeDecision | None] = contextvars.ContextVar(
    "sonder_child_resume_decision", default=None,
)


def resume_receipts() -> Mapping[str, SettledReceipt]:
    """Settled receipts a resumed child runner must consume, by idempotency key.

    Empty outside a resumed runner.  Includes outcomes at or below the
    checkpoint position and those settled after it (for example an effect
    whose receipt committed just before a crash, ahead of the next checkpoint).
    """
    decision = _RESUMED.get()
    if decision is None:
        return _frozen()
    return _frozen({**decision.receipts, **decision.later_receipts})


@contextlib.contextmanager
def resumed_from(decision: CheckpointResumeDecision) -> Iterator[CheckpointResumeDecision]:
    """Bind an allowed resume decision for the runner thread."""
    if not isinstance(decision, CheckpointResumeDecision) or not decision.allowed:
        raise TypeError("resumed_from requires an allowed resume decision")
    token = _RESUMED.set(decision)
    try:
        yield decision
    finally:
        _RESUMED.reset(token)


def validate_uncheckpointed_resume(
    journal: ProvenanceJournal | None,
    *,
    run_id: str,
    worker_id: str,
    resumer_owner_epoch: int,
    admission_operations: frozenset[str],
    page_limit: int = DEFAULT_PAGE_LIMIT,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> CheckpointResumeDecision:
    """Decide whether a child that never checkpointed may restart from empty state.

    Allowed only when the resumer holds the journal's current owner epoch and
    every intent of the run is settled and is one of ``admission_operations``
    (the child's dispatch attempts).  Any other intent means the old runner
    made progress that no checkpoint describes, so restarting from empty state
    could repeat it; that refuses with ``NO_CHECKPOINT``.
    """
    for name, value in (("run_id", run_id), ("worker_id", worker_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} is required")
    if type(resumer_owner_epoch) is not int or resumer_owner_epoch < 1:
        raise ValueError("resumer_owner_epoch must be positive")
    if type(page_limit) is not int or not 1 <= page_limit <= _MAX_PAGE_LIMIT:
        raise ValueError("page_limit must be within 1..10000")
    if type(max_pages) is not int or not 1 <= max_pages <= _MAX_PAGES:
        raise ValueError("max_pages must be within 1..10000")
    R = CheckpointResumeRefusal
    if journal is None:
        return _refuse(R.JOURNAL_MISSING, "no effect journal is available")
    try:
        position = journal.position(run_id, worker_id)
        if position is None:
            return _refuse(R.JOURNAL_MISSING, "effect journal or its identity is missing")
        if position.current_owner_epoch is None or position.current_owner_epoch != resumer_owner_epoch:
            return _refuse(R.STALE_OWNER_EPOCH,
                           "resumer does not hold the journal's current owner epoch")
        read = _read_run(journal, run_id, page_limit=page_limit, max_pages=max_pages)
        if not isinstance(read, CheckpointResumeDecision):
            confirm = journal.position(run_id, worker_id)
            if (confirm is None or confirm.journal_identity != position.journal_identity
                    or confirm.current_owner_epoch != position.current_owner_epoch):
                return _refuse(R.JOURNAL_CHANGED, "effect journal changed while it was being read")
    except EffectJournalError as exc:
        return _refuse(R.JOURNAL_UNAVAILABLE, f"effect journal read failed: {exc}")
    if isinstance(read, CheckpointResumeDecision):
        return read
    records, final_page = read
    receipts: dict[str, SettledReceipt] = {}
    for record in records:
        if record.state in _UNRESOLVED:
            return _refuse(R.UNRESOLVED_AFTER_POSITION,
                           f"journal sequence {record.sequence} requires reconciliation")
        if record.operation_id not in admission_operations:
            return _refuse(R.NO_CHECKPOINT,
                           "child made journaled progress but saved no checkpoint")
        receipts[record.idempotency_key] = _receipt(record)
    if final_page.settled_high_water != final_page.high_water:
        return _refuse(R.JOURNAL_CHANGED, "journal settled high-water moved during validation")
    return CheckpointResumeDecision(
        True, None, "child has no checkpoint and no journaled progress beyond admission",
        settled_position=final_page.high_water, resume_state=MappingProxyType({}),
        receipts=_frozen(receipts),
    )


__all__ = [
    "ChildResumeRefused", "resume_receipts", "resumed_from",
    "CheckpointProvenanceError", "CheckpointProvenanceHook", "CheckpointResumeDecision",
    "CheckpointResumeRefusal", "JournalPosition", "JournalProvenanceStamp",
    "ProvenanceBinding", "ProvenanceJournal", "ProvenanceSubject", "SettledReceipt",
    "validate_checkpoint_resume", "validate_uncheckpointed_resume",
]
