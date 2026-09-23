"""SQLite-backed mutating-effect intent/outcome journal."""
from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import Mapping

from sonder_runtime.adapters.persistence.owned_sqlite import transaction as owned_sqlite_transaction
from sonder_runtime.application.execution.effect_journal import (
    EffectIntent, EffectJournalError, EffectOutcome, EffectState, RecoveryDecision,
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
"""


class SQLiteEffectJournal:
    """Append-only intent ledger with idempotent terminal transitions."""

    def __init__(self, db_path: str | Path, *, max_detail: int = 4096) -> None:
        if type(max_detail) is not int or not 1 <= max_detail <= 1 << 20:
            raise ValueError("max_detail must be within 1..1048576")
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max_detail = max_detail
        self._lock = Lock()
        with self._connect() as connection:
            connection.executescript(_DDL)

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
            return self._row(connection.execute(
                "SELECT intent_id,run_id,worker_id,operation_id,scope,owner_epoch,"
                "idempotency_key,request_digest,reconciliation,sequence,state,"
                "outcome_digest,receipt_key,detail FROM effect_journal WHERE intent_id=?",
                (outcome.intent_id,),
            ).fetchone())

    def uncertain(self, intent_id: str, *, detail: str) -> EffectIntent:
        if not detail.strip():
            raise EffectJournalError("uncertain effect requires detail")
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
            connection.execute(
                "UPDATE effect_journal SET state=?,detail=? WHERE intent_id=?",
                (EffectState.UNCERTAIN.value, detail[:self._max_detail], intent_id),
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

    def validate_checkpoint(self, run_id: str, high_water: int) -> None:
        actual = self.high_water(run_id)
        if type(high_water) is not int or high_water != actual:
            raise EffectJournalError(
                f"checkpoint effect high-water mismatch: expected {high_water}, actual {actual}"
            )
        with self._connect() as connection:
            unresolved = connection.execute(
                "SELECT 1 FROM effect_journal WHERE run_id=? AND state IN (?,?) LIMIT 1",
                (run_id, EffectState.INTENT.value, EffectState.UNCERTAIN.value),
            ).fetchone()
        if unresolved is not None:
            raise EffectJournalError("checkpoint has an admitted effect without a definitive outcome")

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
            attached = tuple(
                str(row[0]) for row in selected
                if live_workers.get(str(row[1])) == int(row[2])
            )
            orphaned = tuple(
                str(row[0]) for row in selected
                if live_workers.get(str(row[1])) != int(row[2])
            )
            for intent_id in orphaned:
                connection.execute(
                    "UPDATE effect_journal SET state=?,detail=? WHERE intent_id=? "
                    "AND state IN (?,?)",
                    (EffectState.UNCERTAIN.value, "owner unavailable during restart", intent_id,
                     EffectState.INTENT.value, EffectState.UNCERTAIN.value),
                )
            if attached:
                action = "reattach"
                detail = "confirmed live owner may continue its existing intent"
            elif orphaned:
                action = "reconcile"
                detail = "unresolved effects require explicit reconciliation"
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
