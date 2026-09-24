"""SQLite-backed mutating-effect intent/outcome journal."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import queue
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Mapping

from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread

from sonder_runtime.adapters.persistence.owned_sqlite import transaction as owned_sqlite_transaction
from sonder_runtime.application.execution.effect_journal import (
    EffectIntent, EffectJournalError, EffectJournalPage, EffectOutcome,
    EffectState, ReconciliationProof, RecoveryDecision,
)


_DDL = """
CREATE TABLE IF NOT EXISTS effect_journal (
    intent_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    owner_epoch INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    reconciliation TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    state TEXT NOT NULL,
    outcome_digest TEXT NOT NULL DEFAULT '',
    receipt_key TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    UNIQUE(run_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_effect_journal_run_sequence
    ON effect_journal(run_id, sequence);
CREATE INDEX IF NOT EXISTS ix_effect_journal_run_state
    ON effect_journal(run_id, state);
CREATE TABLE IF NOT EXISTS effect_checkpoint (
    run_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    effect_high_water INTEGER NOT NULL,
    state_digest TEXT NOT NULL,
    state_json TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, generation)
);
CREATE INDEX IF NOT EXISTS ix_effect_checkpoint_latest
    ON effect_checkpoint(run_id, generation DESC);
CREATE TABLE IF NOT EXISTS effect_owner (
    run_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    owner_epoch INTEGER NOT NULL,
    recovery_required INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, worker_id)
);
"""

_VERIFIER_SLOTS = threading.BoundedSemaphore(4)
# Checkpoint generations retained per run; older rows are pruned on append.
CHECKPOINT_RETENTION = 16


class SQLiteEffectJournal:
    """Append-only intent ledger with idempotent terminal transitions."""

    def __init__(
        self, db_path: str | Path, *, max_detail: int = 4096,
        reconciliation_verifiers: Mapping[str, object] | None = None,
    ) -> None:
        if type(max_detail) is not int or not 1 <= max_detail <= 1 << 20:
            raise ValueError("max_detail must be within 1..1048576")
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max_detail = max_detail
        self._lock = Lock()
        self._live_latches: set[str] = set()
        # This registry is immutable after construction. Production bootstrap
        # intentionally supplies none until a real provider verifier exists.
        self._reconciliation_verifiers = MappingProxyType(dict(reconciliation_verifiers or {}))
        for operation_id, verifier in self._reconciliation_verifiers.items():
            if (
                type(operation_id) is not str or not operation_id.strip()
                or getattr(verifier, "verifier_id", None) is None
                or not callable(getattr(verifier, "verify", None))
            ):
                raise EffectJournalError("invalid immutable reconciliation verifier registry")
        with self._connect() as connection:
            connection.executescript(_DDL)
            columns = {
                str(row[1]) for row in connection.execute(
                    "PRAGMA table_info(effect_checkpoint)"
                ).fetchall()
            }
            if "state_json" not in columns:
                connection.execute(
                    "ALTER TABLE effect_checkpoint ADD COLUMN state_json TEXT NOT NULL DEFAULT ''"
                )
            owner_columns = {
                str(row[1]) for row in connection.execute(
                    "PRAGMA table_info(effect_owner)"
                ).fetchall()
            }
            if "recovery_required" not in owner_columns:
                connection.execute(
                    "ALTER TABLE effect_owner ADD COLUMN recovery_required INTEGER NOT NULL DEFAULT 0"
                )

    @property
    def database_path(self) -> Path:
        return self._path

    @contextmanager
    def _connect(self):
        with owned_sqlite_transaction(str(self._path), timeout=5.0) as connection:
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection

    @staticmethod
    def _row(row) -> EffectIntent | None:
        if row is None:
            return None
        return EffectIntent(
            str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]),
            int(row[5]), str(row[6]), str(row[7]), str(row[8]), int(row[9]),
            EffectState(str(row[10])), str(row[11]), str(row[12]), str(row[13]),
        )

    def begin(self, intent: EffectIntent) -> EffectIntent:
        if not isinstance(intent, EffectIntent):
            raise TypeError("intent must be an EffectIntent")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_owner_in_transaction(
                connection, intent.run_id, intent.worker_id, intent.owner_epoch,
            )
            existing = self._row(connection.execute(
                "SELECT intent_id,run_id,worker_id,operation_id,scope,owner_epoch,"
                "idempotency_key,request_digest,reconciliation,sequence,state,"
                "outcome_digest,receipt_key,detail FROM effect_journal WHERE intent_id=?",
                (intent.intent_id,),
            ).fetchone())
            if existing is None:
                prior = self._row(connection.execute(
                    "SELECT intent_id,run_id,worker_id,operation_id,scope,owner_epoch,"
                    "idempotency_key,request_digest,reconciliation,sequence,state,"
                    "outcome_digest,receipt_key,detail FROM effect_journal "
                    "WHERE run_id=? AND idempotency_key=?",
                    (intent.run_id, intent.idempotency_key),
                ).fetchone())
                if prior is not None:
                    if (
                        prior.intent_id, prior.run_id, prior.worker_id,
                        prior.operation_id, prior.scope, prior.owner_epoch,
                        prior.idempotency_key, prior.request_digest, prior.reconciliation,
                    ) != (
                        intent.intent_id, intent.run_id, intent.worker_id,
                        intent.operation_id, intent.scope, intent.owner_epoch,
                        intent.idempotency_key, intent.request_digest, intent.reconciliation,
                    ):
                        raise EffectJournalError("idempotency key conflicts with admitted effect identity")
                    return replace(prior, replayed=True)
                sequence = int(connection.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM effect_journal WHERE run_id=?",
                    (intent.run_id,),
                ).fetchone()[0])
                connection.execute(
                    "INSERT INTO effect_journal(intent_id,run_id,worker_id,operation_id,scope,"
                    "owner_epoch,idempotency_key,request_digest,reconciliation,sequence,state) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (intent.intent_id, intent.run_id, intent.worker_id, intent.operation_id,
                     intent.scope, intent.owner_epoch, intent.idempotency_key,
                     intent.request_digest, intent.reconciliation, sequence, EffectState.INTENT.value),
                )
                return self._row(connection.execute(
                    "SELECT intent_id,run_id,worker_id,operation_id,scope,owner_epoch,"
                    "idempotency_key,request_digest,reconciliation,sequence,state,"
                    "outcome_digest,receipt_key,detail FROM effect_journal WHERE intent_id=?",
                    (intent.intent_id,),
                ).fetchone())
            if (
                existing.run_id, existing.worker_id, existing.operation_id,
                existing.scope, existing.owner_epoch, existing.idempotency_key,
                existing.request_digest, existing.reconciliation,
            ) != (
                intent.run_id, intent.worker_id, intent.operation_id,
                intent.scope, intent.owner_epoch, intent.idempotency_key,
                intent.request_digest, intent.reconciliation,
            ):
                raise EffectJournalError("intent identity conflicts with durable record")
            return replace(existing, replayed=True)

    def _ensure_owner_in_transaction(self, connection, run_id: str, worker_id: str, owner_epoch: int) -> None:
        if run_id in self._live_latches:
            raise EffectJournalError(
                "worker owner recovery is required; unresolved effects require reconciliation before admitting new effects"
            )
        row = connection.execute(
            "SELECT owner_epoch,recovery_required FROM effect_owner WHERE run_id=? AND worker_id=?",
            (run_id, worker_id),
        ).fetchone()
        if row is None:
            latched = connection.execute(
                "SELECT 1 FROM effect_owner WHERE run_id=? AND recovery_required=1 LIMIT 1",
                (run_id,),
            ).fetchone()
            if latched is not None:
                raise EffectJournalError(
                    "worker owner recovery is required; unresolved effects require reconciliation before admitting new effects"
                )
            connection.execute(
                "INSERT INTO effect_owner(run_id,worker_id,owner_epoch) VALUES(?,?,?)",
                (run_id, worker_id, owner_epoch),
            )
            return
        current_epoch = int(row[0])
        if current_epoch > owner_epoch:
            raise EffectJournalError("stale worker owner epoch")
        if int(row[1]):
            raise EffectJournalError(
                "duplicate effect intent requires reconciliation before admitting new effects"
            )
        if current_epoch == owner_epoch:
            return
        unresolved = connection.execute(
            "SELECT 1 FROM effect_journal WHERE run_id=? AND state IN (?,?) LIMIT 1",
            (run_id, EffectState.INTENT.value, EffectState.UNCERTAIN.value),
        ).fetchone()
        if unresolved is not None:
            raise EffectJournalError("worker owner recovery is required before new effects")
        connection.execute(
            "UPDATE effect_owner SET owner_epoch=? WHERE run_id=? AND worker_id=?",
            (owner_epoch, run_id, worker_id),
        )

    def claim_owner(self, run_id: str, worker_id: str, owner_epoch: int) -> None:
        """Durably advance the current owner before restart recovery runs."""
        if not all(isinstance(value, str) and value.strip() for value in (run_id, worker_id)):
            raise EffectJournalError("owner identity is required")
        if type(owner_epoch) is not int or owner_epoch < 1:
            raise EffectJournalError("owner epoch must be positive")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_epoch FROM effect_owner WHERE run_id=? AND worker_id=?",
                (run_id, worker_id),
            ).fetchone()
            if row is not None:
                current_epoch = int(row[0])
                if owner_epoch < current_epoch:
                    raise EffectJournalError("stale worker owner epoch")
                if owner_epoch == current_epoch:
                    return
            if row is None:
                connection.execute(
                    "INSERT INTO effect_owner(run_id,worker_id,owner_epoch,recovery_required) VALUES(?,?,?,0)",
                    (run_id, worker_id, owner_epoch),
                )
            else:
                connection.execute(
                    "UPDATE effect_owner SET owner_epoch=? WHERE run_id=? AND worker_id=?",
                    (owner_epoch, run_id, worker_id),
                )

    def reconcile(self, intent_id: str, *, owner_epoch: int, timeout_seconds: float = 2.0) -> EffectIntent:
        """Apply one registered verifier result and clear the fence atomically.

        The owner epoch is checked both before and during the transaction.
        Unsupported operation families stay fenced; a stale reconciler cannot
        clear a newer owner's recovery requirement.
        """
        if type(owner_epoch) is not int or owner_epoch < 1:
            raise EffectJournalError("reconciliation owner epoch must be positive")
        if type(intent_id) is not str or not intent_id.strip():
            raise EffectJournalError("reconciliation intent_id is required")
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise EffectJournalError("reconciliation timeout must be within 0..30 seconds")
        # Obtain the external proof without holding the SQLite lock or a
        # write transaction. A hung provider can delay its own reconciliation
        # but cannot block ordinary journal writes.
        with self._lock, self._connect() as connection:
            snapshot = self._row(connection.execute(
                "SELECT intent_id,run_id,worker_id,operation_id,scope,owner_epoch,"
                "idempotency_key,request_digest,reconciliation,sequence,state,"
                "outcome_digest,receipt_key,detail FROM effect_journal WHERE intent_id=?",
                (intent_id,),
            ).fetchone())
            if snapshot is None:
                raise KeyError(intent_id)
            if snapshot.state in {EffectState.COMPLETED, EffectState.FAILED}:
                return snapshot
            verifier = self._reconciliation_verifiers.get(snapshot.operation_id)
            if verifier is None:
                for family, candidate in self._reconciliation_verifiers.items():
                    if snapshot.operation_id.startswith(family + ":"):
                        verifier = candidate
                        break
            if verifier is None:
                raise EffectJournalError(
                    f"no trusted reconciliation verifier for {snapshot.operation_id}"
                )
        if not _VERIFIER_SLOTS.acquire(blocking=False):
            raise EffectJournalError("host reconciliation verifier capacity exhausted")
        result_queue = queue.Queue(maxsize=1)

        def run_verifier() -> None:
            try:
                result_queue.put((True, verifier.verify(snapshot)))
            except BaseException as exc:  # transport/provider failures are fenced
                result_queue.put((False, exc))
            finally:
                _VERIFIER_SLOTS.release()

        verifier_thread = owned_runtime_thread(
            target=run_verifier, name="sonder-effect-verifier", daemon=True,
        )
        verifier_thread.start()
        verifier_thread.join(float(timeout_seconds))
        if verifier_thread.is_alive():
            raise EffectJournalError("host reconciliation verifier timed out")
        succeeded, proof = result_queue.get_nowait()
        if not succeeded:
            raise EffectJournalError("host reconciliation verifier failed") from proof
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._row(connection.execute(
                "SELECT intent_id,run_id,worker_id,operation_id,scope,owner_epoch,"
                "idempotency_key,request_digest,reconciliation,sequence,state,"
                "outcome_digest,receipt_key,detail FROM effect_journal WHERE intent_id=?",
                (intent_id,),
            ).fetchone())
            if current is None:
                raise KeyError(intent_id)
            if current.state in {EffectState.COMPLETED, EffectState.FAILED}:
                return current
            if current != snapshot:
                raise EffectJournalError("reconciliation proof was obtained from stale effect state")
            owner = connection.execute(
                "SELECT owner_epoch,recovery_required FROM effect_owner "
                "WHERE run_id=? AND worker_id=?",
                (current.run_id, current.worker_id),
            ).fetchone()
            if owner is None or int(owner[0]) != owner_epoch:
                raise EffectJournalError("stale reconciliation owner epoch")
            if type(proof) is not ReconciliationProof:
                raise EffectJournalError("host verifier returned no trusted proof")
            if (
                proof.intent_id != current.intent_id
                or proof.operation_id != current.operation_id
                or proof.state not in {EffectState.COMPLETED, EffectState.FAILED}
            ):
                raise EffectJournalError("reconciliation proof identity conflict")
            changed = connection.execute(
                "UPDATE effect_journal SET state=?,outcome_digest=?,receipt_key=?,detail=? "
                "WHERE intent_id=? AND state=?",
                (proof.state.value, proof.outcome_digest, proof.receipt_key,
                 f"verified:{proof.verifier_id}:{proof.external_reference}"[:self._max_detail],
                 current.intent_id, current.state.value),
            ).rowcount
            if changed != 1:
                raise EffectJournalError("reconciliation lost its effect race")
            unresolved = connection.execute(
                "SELECT 1 FROM effect_journal WHERE run_id=? AND state IN (?,?) LIMIT 1",
                (current.run_id, EffectState.INTENT.value, EffectState.UNCERTAIN.value),
            ).fetchone()
            if unresolved is None:
                # recover() fences every owner in the run; once no effect in
                # the run is unresolved, every owner's fence is released, not
                # only the reconciled intent's worker.
                connection.execute(
                    "UPDATE effect_owner SET recovery_required=0 WHERE run_id=?",
                    (current.run_id,),
                )
                connection.commit()  # Commit recovery before releasing the live fence.
                self._live_latches.discard(current.run_id)
            return self._row(connection.execute(
                "SELECT intent_id,run_id,worker_id,operation_id,scope,owner_epoch,"
                "idempotency_key,request_digest,reconciliation,sequence,state,"
                "outcome_digest,receipt_key,detail FROM effect_journal WHERE intent_id=?",
                (current.intent_id,),
            ).fetchone())

    def get(self, intent_id: str) -> EffectIntent | None:
        with self._connect() as connection:
            return self._row(connection.execute(
                "SELECT intent_id,run_id,worker_id,operation_id,scope,owner_epoch,"
                "idempotency_key,request_digest,reconciliation,sequence,state,"
                "outcome_digest,receipt_key,detail FROM effect_journal WHERE intent_id=?",
                (intent_id,),
            ).fetchone())

    def outcome(self, outcome: EffectOutcome) -> EffectIntent:
        if not isinstance(outcome, EffectOutcome):
            raise TypeError("outcome must be an EffectOutcome")
        if len(outcome.detail) > self._max_detail:
            raise EffectJournalError("outcome detail exceeds bound")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._row(connection.execute(
                "SELECT intent_id,run_id,worker_id,operation_id,scope,owner_epoch,"
                "idempotency_key,request_digest,reconciliation,sequence,state,"
                "outcome_digest,receipt_key,detail FROM effect_journal WHERE intent_id=?",
                (outcome.intent_id,),
            ).fetchone())
            if current is None:
                raise KeyError(outcome.intent_id)
            if (current.worker_id, current.owner_epoch) != (
                outcome.worker_id, outcome.owner_epoch
            ):
                raise EffectJournalError("outcome owner does not match admitted intent")
            if current.state in {EffectState.COMPLETED, EffectState.FAILED}:
                if (current.state, current.outcome_digest, current.receipt_key) != (
                    outcome.state, outcome.outcome_digest, outcome.receipt_key):
                    raise EffectJournalError("terminal effect outcome conflict")
                self._commit_settled_run(connection, current.run_id)
                return current
            if current.state is EffectState.UNCERTAIN:
                raise EffectJournalError(
                    "uncertain effect requires explicit reconciliation; late receipt refused"
                )
            connection.execute(
                "UPDATE effect_journal SET state=?,outcome_digest=?,receipt_key=?,detail=? "
                "WHERE intent_id=? AND state=?",
                (outcome.state.value, outcome.outcome_digest, outcome.receipt_key,
                 outcome.detail[:self._max_detail], outcome.intent_id, current.state.value),
            )
            result = self._row(connection.execute(
                "SELECT intent_id,run_id,worker_id,operation_id,scope,owner_epoch,"
                "idempotency_key,request_digest,reconciliation,sequence,state,"
                "outcome_digest,receipt_key,detail FROM effect_journal WHERE intent_id=?",
                (outcome.intent_id,),
            ).fetchone())

            self._commit_settled_run(connection, current.run_id)
            return result

    def _commit_settled_run(self, connection, run_id: str) -> None:
        """Release an outage fence only after every effect is durably terminal.

        Called with the admission lock held, after all writes in the caller's
        transaction. A pending sibling keeps the whole run fenced.
        """
        if run_id not in self._live_latches:
            return
        unresolved = connection.execute(
            "SELECT 1 FROM effect_journal WHERE run_id=? AND state IN (?,?) LIMIT 1",
            (run_id, EffectState.INTENT.value, EffectState.UNCERTAIN.value),
        ).fetchone()
        if unresolved is not None:
            return
        connection.execute("UPDATE effect_owner SET recovery_required=0 WHERE run_id=?", (run_id,))
        connection.commit()
        self._live_latches.discard(run_id)

    def _apply_outcome_in_transaction(self, connection, outcome: EffectOutcome) -> EffectIntent:
        current = self._row(connection.execute(
            "SELECT intent_id,run_id,worker_id,operation_id,scope,owner_epoch,"
            "idempotency_key,request_digest,reconciliation,sequence,state,"
            "outcome_digest,receipt_key,detail FROM effect_journal WHERE intent_id=?",
            (outcome.intent_id,),
        ).fetchone())
        if current is None:
            raise KeyError(outcome.intent_id)
        if (current.worker_id, current.owner_epoch) != (
            outcome.worker_id, outcome.owner_epoch
        ):
            raise EffectJournalError("outcome owner does not match admitted intent")
        if current.state in {EffectState.COMPLETED, EffectState.FAILED}:
            if (current.state, current.outcome_digest, current.receipt_key) != (
                outcome.state, outcome.outcome_digest, outcome.receipt_key
            ):
                raise EffectJournalError("terminal effect outcome conflict")
            # Idempotent replay: signal it so no new checkpoint is appended.
            return replace(current, replayed=True)
        if current.state is EffectState.UNCERTAIN:
            raise EffectJournalError(
                "uncertain effect requires explicit reconciliation; late receipt refused"
            )
        connection.execute(
            "UPDATE effect_journal SET state=?,outcome_digest=?,receipt_key=?,detail=? "
            "WHERE intent_id=? AND state=?",
            (outcome.state.value, outcome.outcome_digest, outcome.receipt_key,
             outcome.detail[:self._max_detail], outcome.intent_id, current.state.value),
        )
        return self._row(connection.execute(
            "SELECT intent_id,run_id,worker_id,operation_id,scope,owner_epoch,"
            "idempotency_key,request_digest,reconciliation,sequence,state,"
            "outcome_digest,receipt_key,detail FROM effect_journal WHERE intent_id=?",
            (outcome.intent_id,),
        ).fetchone())

    @staticmethod
    def _require_current_owner_in_transaction(
        connection, run_id: str, worker_id: str, owner_epoch: int,
    ) -> None:
        """Checkpoint writers must be the run's current, unfenced owner."""
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise EffectJournalError("checkpoint writer owner identity is required")
        if type(owner_epoch) is not int or owner_epoch < 1:
            raise EffectJournalError("checkpoint writer owner epoch must be positive")
        row = connection.execute(
            "SELECT owner_epoch,recovery_required FROM effect_owner WHERE run_id=? AND worker_id=?",
            (run_id, worker_id),
        ).fetchone()
        if row is None:
            raise EffectJournalError("checkpoint writer is not an admitted owner of the run")
        if int(row[0]) > owner_epoch:
            raise EffectJournalError("stale worker owner epoch")
        if int(row[0]) != owner_epoch:
            raise EffectJournalError("checkpoint writer owner epoch was never claimed")
        if int(row[1]):
            raise EffectJournalError("recovery is required before writing a checkpoint")

    def uncertain(self, intent_id: str, *, detail: str, run_id: str | None = None) -> EffectIntent:
        return self._record_uncertainty(intent_id, detail=detail, run_id=run_id)

    def latch_uncertainty(self, intent_id: str, *, detail: str, run_id: str | None = None) -> EffectIntent:
        """Retain the same admission fence if normal publication failed."""
        return self._record_uncertainty(intent_id, detail=detail, run_id=run_id)

    def _record_uncertainty(self, intent_id: str, *, detail: str, run_id: str | None) -> EffectIntent:
        if not detail.strip():
            raise EffectJournalError("uncertain effect requires detail")
        with self._lock:
            # The authenticated binding supplies run_id before any storage
            # access. Keep this fence even if connect/BEGIN/SELECT/commit fail;
            # begin() uses this same lock and cannot admit between these steps.
            was_latched = run_id in self._live_latches
            if run_id is not None:
                self._live_latches.add(run_id)
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self._row(connection.execute(
                    "SELECT intent_id,run_id,worker_id,operation_id,scope,owner_epoch,"
                    "idempotency_key,request_digest,reconciliation,sequence,state,"
                    "outcome_digest,receipt_key,detail FROM effect_journal WHERE intent_id=?",
                    (intent_id,),
                ).fetchone())
                if current is None:
                    raise KeyError(intent_id)
                if run_id is not None and current.run_id != run_id:
                    raise EffectJournalError("uncertainty run identity conflict")
                if current.state in {EffectState.COMPLETED, EffectState.FAILED}:
                    # An acknowledgement can fail after a definitive commit.
                    # Undo only this call's provisional fence, never a fence
                    # already raised by another unresolved effect.
                    if run_id is not None and not was_latched:
                        self._live_latches.discard(run_id)
                    return current
                self._live_latches.add(current.run_id)
                connection.execute(
                    "UPDATE effect_journal SET state=?,detail=? WHERE intent_id=?",
                    (EffectState.UNCERTAIN.value, detail[:self._max_detail], intent_id),
                )
                connection.execute(
                    "UPDATE effect_owner SET recovery_required=1 WHERE run_id=?",
                    (current.run_id,),
                )
                return self._row(connection.execute(
                    "SELECT intent_id,run_id,worker_id,operation_id,scope,owner_epoch,"
                    "idempotency_key,request_digest,reconciliation,sequence,state,"
                    "outcome_digest,receipt_key,detail FROM effect_journal WHERE intent_id=?",
                    (intent_id,),
                ).fetchone())

    def high_water(self, run_id: str) -> int:
        with self._connect() as connection:
            return int(connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM effect_journal WHERE run_id=?",
                (run_id,),
            ).fetchone()[0])

    @staticmethod
    def _settled_high_water_in(connection, run_id: str) -> int:
        # Sequences are allocated contiguously per run (MAX+1 under BEGIN
        # IMMEDIATE), so the settled prefix ends just before the first
        # intent that has no definitive outcome.
        first_unresolved = connection.execute(
            "SELECT MIN(sequence) FROM effect_journal WHERE run_id=? AND state IN (?,?)",
            (run_id, EffectState.INTENT.value, EffectState.UNCERTAIN.value),
        ).fetchone()[0]
        if first_unresolved is not None:
            return int(first_unresolved) - 1
        return int(connection.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM effect_journal WHERE run_id=?",
            (run_id,),
        ).fetchone()[0])

    def settled_high_water(self, run_id: str) -> int:
        """Return the largest sequence whose whole prefix is terminal.

        Every intent of ``run_id`` with ``sequence <= result`` is COMPLETED or
        FAILED; the intent at ``result + 1`` (if any) is INTENT or UNCERTAIN.
        Returns 0 for a run with no intents or whose first intent is
        unresolved.  Read-only: no ownership claim, fence or state change.
        A checkpoint that records this value binds exactly the effects whose
        outcomes it may rely on.
        """
        if not isinstance(run_id, str) or not run_id.strip():
            raise EffectJournalError("run_id is required")
        with self._connect() as connection:
            connection.execute("BEGIN")
            return self._settled_high_water_in(connection, run_id)

    def effects_since(
        self, run_id: str, after_sequence: int, *, limit: int = 100,
        worker_id: str | None = None,
    ) -> EffectJournalPage:
        """Return a bounded snapshot of intents after a checkpoint position.

        ``after_sequence`` is normally the ``effect_high_water`` a checkpoint
        stored (0 for none).  Records are returned in sequence order with
        their current state, receipt key and idempotency key, so a resuming
        worker can map settled effects to stored receipts by idempotency key
        without re-invoking them, and see unresolved ones that require
        reconciliation.  ``worker_id`` narrows ``records`` only; high-water
        values always describe the whole run.  Read-only.
        """
        if not isinstance(run_id, str) or not run_id.strip():
            raise EffectJournalError("run_id is required")
        if type(after_sequence) is not int or after_sequence < 0:
            raise EffectJournalError("after_sequence must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise EffectJournalError("limit must be within 1..10000")
        if worker_id is not None and (not isinstance(worker_id, str) or not worker_id.strip()):
            raise EffectJournalError("worker_id must be non-empty when supplied")
        query = (
            "SELECT intent_id,run_id,worker_id,operation_id,scope,owner_epoch,"
            "idempotency_key,request_digest,reconciliation,sequence,state,"
            "outcome_digest,receipt_key,detail FROM effect_journal "
            "WHERE run_id=? AND sequence>?"
        )
        params: list[object] = [run_id, after_sequence]
        if worker_id is not None:
            query += " AND worker_id=?"
            params.append(worker_id)
        query += " ORDER BY sequence LIMIT ?"
        params.append(limit + 1)
        with self._connect() as connection:
            # One deferred read transaction: records and both high-water
            # values come from the same snapshot.
            connection.execute("BEGIN")
            rows = connection.execute(query, params).fetchall()
            high_water = int(connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM effect_journal WHERE run_id=?",
                (run_id,),
            ).fetchone()[0])
            settled = self._settled_high_water_in(connection, run_id)
        records = tuple(self._row(row) for row in rows[:limit])
        return EffectJournalPage(
            run_id, after_sequence, records, high_water, settled,
            truncated=len(rows) > limit,
        )

    @staticmethod
    def _encode_state(state: object) -> tuple[str, str]:
        try:
            encoded = json.dumps(
                state, sort_keys=True, separators=(",", ":"),
                ensure_ascii=True, allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise EffectJournalError("checkpoint state must be JSON serializable") from exc
        if len(encoded.encode("utf-8")) > 1 << 20:
            raise EffectJournalError("checkpoint state exceeds 1 MiB")
        return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _append_checkpoint_in_transaction(
        self, connection, run_id: str, state: object,
        encoded: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        """Append one generation bound to the run's settled high-water.

        Concurrent effects in one run are normal (process and compute workers
        share a run id), so an in-flight intent does not refuse the
        checkpoint.  The checkpoint binds only the settled prefix: every
        effect at or below ``effect_high_water`` has a definitive outcome.
        """
        state_json, state_digest = encoded if encoded is not None else self._encode_state(state)
        latest = connection.execute(
            "SELECT generation,effect_high_water FROM effect_checkpoint WHERE run_id=? "
            "ORDER BY generation DESC LIMIT 1", (run_id,),
        ).fetchone()
        generation = -1 if latest is None else int(latest[0])
        high_water = self._settled_high_water_in(connection, run_id)
        if latest is not None and high_water < int(latest[1]):
            raise EffectJournalError("settled effect high-water moved backwards")
        generation += 1
        connection.execute(
            "INSERT INTO effect_checkpoint(run_id,generation,effect_high_water,state_digest,state_json) "
            "VALUES(?,?,?,?,?)",
            (run_id, generation, high_water, state_digest, state_json),
        )
        connection.execute(
            "DELETE FROM effect_checkpoint WHERE run_id=? AND generation<=?",
            (run_id, generation - CHECKPOINT_RETENTION),
        )
        return {
            "run_id": run_id,
            "generation": generation,
            "effect_high_water": high_water,
            "state_digest": state_digest,
            "state": state,
        }

    def outcome_and_checkpoint(
        self, outcome: EffectOutcome, state: object,
    ) -> dict[str, object] | None:
        """Commit a terminal outcome and serialized worker state together.

        The outcome's worker and epoch must still be the run's current,
        unfenced owner.  An idempotent replay of an already-recorded outcome
        appends no checkpoint and returns ``None``.
        """
        if not isinstance(outcome, EffectOutcome):
            raise TypeError("outcome must be an EffectOutcome")
        if len(outcome.detail) > self._max_detail:
            raise EffectJournalError("outcome detail exceeds bound")
        encoded = self._encode_state(state)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stored = self._apply_outcome_in_transaction(connection, outcome)
            self._require_current_owner_in_transaction(
                connection, stored.run_id, outcome.worker_id, int(outcome.owner_epoch),
            )
            if stored.replayed:
                self._commit_settled_run(connection, stored.run_id)
                return None
            checkpoint = self._append_checkpoint_in_transaction(
                connection, stored.run_id, state, encoded,
            )
            self._commit_settled_run(connection, stored.run_id)
            return checkpoint

    def append_checkpoint(
        self, run_id: str, state: object, *, worker_id: str, owner_epoch: int,
    ) -> dict[str, object]:
        """Persist a worker checkpoint atomically with its journal high-water.

        The generation and settled high-water are allocated by the host-owned
        SQLite transaction; a caller cannot supply either.  The writer must be
        the run's current owner at ``owner_epoch`` with no recovery fence, so
        a stale owner cannot overwrite a newer owner's restore point.
        """
        if not isinstance(run_id, str) or not run_id.strip():
            raise EffectJournalError("checkpoint run_id is required")
        encoded = self._encode_state(state)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_current_owner_in_transaction(
                connection, run_id, worker_id, owner_epoch,
            )
            return self._append_checkpoint_in_transaction(connection, run_id, state, encoded)

    def restore_checkpoint(self, run_id: str) -> dict[str, object] | None:
        """Return the latest checkpoint when every journal effect is settled.

        A checkpoint binds its settled prefix (``effect_high_water``).  Later
        effects may exist, for example one resolved by verified
        reconciliation after a crash; they are accepted only when every
        effect in the run is terminal, and ``journal_high_water`` tells the
        caller to read them with ``effects_since(run_id, effect_high_water)``.
        Any unresolved effect, or a checkpoint ahead of the journal, refuses.
        """
        if not isinstance(run_id, str) or not run_id.strip():
            raise EffectJournalError("checkpoint run_id is required")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT generation,effect_high_water,state_digest,state_json FROM effect_checkpoint "
                "WHERE run_id=? ORDER BY generation DESC LIMIT 1", (run_id,),
            ).fetchone()
            if row is None:
                return None
            current = int(connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM effect_journal WHERE run_id=?",
                (run_id,),
            ).fetchone()[0])
            if int(row[1]) > current:
                raise EffectJournalError(
                    "checkpoint effect high-water is ahead of the journal: "
                    f"checkpoint={int(row[1])}, journal={current}"
                )
            unresolved = connection.execute(
                "SELECT 1 FROM effect_journal WHERE run_id=? AND state IN (?,?) LIMIT 1",
                (run_id, EffectState.INTENT.value, EffectState.UNCERTAIN.value),
            ).fetchone()
            if unresolved is not None:
                raise EffectJournalError(
                    "checkpoint has an admitted effect without a definitive outcome"
                )
            return {
                "run_id": run_id,
                "generation": int(row[0]),
                "effect_high_water": int(row[1]),
                "journal_high_water": current,
                "state_digest": str(row[2]),
                "state": json.loads(str(row[3])) if str(row[3]) else None,
            }

    def validate_checkpoint(self, run_id: str, high_water: int) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN")
            self.validate_checkpoint_in_transaction(connection, run_id, high_water)

    def validate_checkpoint_in_transaction(self, connection, run_id: str, high_water: int) -> int:
        """Validate one effect snapshot using the checkpoint caller's connection."""
        if not connection.in_transaction:
            raise EffectJournalError("checkpoint effect validation needs a transaction")
        actual = int(connection.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM effect_journal WHERE run_id=?",
            (run_id,),
        ).fetchone()[0])
        if type(high_water) is not int or high_water != actual:
            raise EffectJournalError(
                f"checkpoint effect high-water mismatch: expected {high_water}, actual {actual}"
            )
        unresolved = connection.execute(
            "SELECT 1 FROM effect_journal WHERE run_id=? AND state IN (?,?) LIMIT 1",
            (run_id, EffectState.INTENT.value, EffectState.UNCERTAIN.value),
        ).fetchone()
        if unresolved is not None:
            raise EffectJournalError("checkpoint has an admitted effect without a definitive outcome")
        return actual

    def recover(self, run_id: str, *, live_workers: Mapping[str, int], max_records: int = 100) -> RecoveryDecision:
        if isinstance(max_records, bool) or not 1 <= max_records <= 10_000:
            raise ValueError("max_records must be within 1..10000")
        if not isinstance(live_workers, Mapping) or any(
            not isinstance(worker, str) or not worker
            or type(epoch) is not int or epoch < 1
            for worker, epoch in live_workers.items()
        ):
            raise ValueError("live worker epochs must be an authenticated mapping")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT intent_id,worker_id,owner_epoch,state FROM effect_journal WHERE run_id=? "
                "AND state IN (?,?) ORDER BY sequence LIMIT ?",
                (run_id, EffectState.INTENT.value, EffectState.UNCERTAIN.value, max_records + 1),
            ).fetchall()
            if len(rows) > max_records:
                raise EffectJournalError("effect recovery exceeds bounded page")
            selected = rows
            # An intent that was never observed past its admission point may
            # be reattached to the exact live owner.  Once an effect has been
            # marked UNCERTAIN, however, the process may have crossed the
            # external side-effect boundary before it died.  Treating that
            # row as reattachable would let a recovered worker invoke it a
            # second time.  Uncertainty therefore remains a reconciliation
            # requirement even when the old worker identity still appears
            # live.
            attached = tuple(
                str(row[0]) for row in selected
                if str(row[3]) == EffectState.INTENT.value
                and live_workers.get(str(row[1])) == int(row[2])
            )
            orphaned = tuple(
                str(row[0]) for row in selected
                if str(row[3]) != EffectState.INTENT.value
                or live_workers.get(str(row[1])) != int(row[2])
            )
            for intent_id in orphaned:
                connection.execute(
                    "UPDATE effect_journal SET state=?,detail=? WHERE intent_id=? "
                    "AND state IN (?,?)",
                    (EffectState.UNCERTAIN.value, "effect requires reconciliation after restart", intent_id,
                     EffectState.INTENT.value, EffectState.UNCERTAIN.value),
                )
            if orphaned:
                self._live_latches.add(run_id)
                connection.execute(
                    "UPDATE effect_owner SET recovery_required=1 WHERE run_id=?",
                    (run_id,),
                )
                action = "reconcile"
                detail = "unresolved effects require explicit reconciliation"
            elif attached:
                action = "reattach"
                detail = "confirmed live owner may continue its existing intent"
            else:
                action = "resume"
                detail = "no unresolved effects"
            high_water = int(connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM effect_journal WHERE run_id=?",
                (run_id,),
            ).fetchone()[0])
            return RecoveryDecision(run_id, action, attached + orphaned,
                                    high_water, detail)


__all__ = ["SQLiteEffectJournal"]
