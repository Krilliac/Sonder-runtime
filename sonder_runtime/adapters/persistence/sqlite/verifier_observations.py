"""Append-only SQLite persistence for host-authenticated verifier observations."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from ....application.memory.learning_ladder import LearningObservation
from ....application.memory.receipt_observation import (
    VerifierReceipt,
    _observation_payload,
    _receipt_payload,
)


_TABLE_DDL = """CREATE TABLE IF NOT EXISTS verifier_learning_observations (
    observation_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL UNIQUE,
    receipt_json TEXT NOT NULL,
    observation_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);"""
_UPDATE_TRIGGER_DDL = """CREATE TRIGGER IF NOT EXISTS verifier_learning_observations_immutable_update
BEFORE UPDATE ON verifier_learning_observations BEGIN
    SELECT RAISE(ABORT, 'verifier learning observations are immutable');
END;"""
_DELETE_TRIGGER_DDL = """CREATE TRIGGER IF NOT EXISTS verifier_learning_observations_immutable_delete
BEFORE DELETE ON verifier_learning_observations BEGIN
    SELECT RAISE(ABORT, 'verifier learning observations are immutable');
END;"""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _observation(value: dict) -> LearningObservation:
    from datetime import datetime
    value = dict(value)
    value["observed_at"] = datetime.fromisoformat(value["observed_at"])
    value["provenance"] = tuple(value["provenance"])
    return LearningObservation(**value)


class SQLiteVerifierObservationRepository:
    """Persist and replay receipt/observation pairs on one caller-owned connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        for statement in (_TABLE_DDL, _UPDATE_TRIGGER_DDL, _DELETE_TRIGGER_DDL):
            self._connection.execute(statement)

    @contextmanager
    def _transaction(self):
        nested = bool(self._connection.in_transaction)
        if nested:
            self._connection.execute("SAVEPOINT sonder_verifier_observation_write")
        else:
            self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            if nested:
                self._connection.execute("ROLLBACK TO SAVEPOINT sonder_verifier_observation_write")
                self._connection.execute("RELEASE SAVEPOINT sonder_verifier_observation_write")
            else:
                self._connection.rollback()
            raise
        else:
            if nested:
                self._connection.execute("RELEASE SAVEPOINT sonder_verifier_observation_write")
            else:
                self._connection.commit()

    def append(self, receipt: VerifierReceipt, observation: LearningObservation) -> LearningObservation:
        receipt_payload = _json(_receipt_payload(receipt))
        observation_payload = _json(_observation_payload(observation))
        with self._transaction():
            row = self._connection.execute(
                "SELECT observation_id, receipt_id, receipt_json, observation_json "
                "FROM verifier_learning_observations WHERE observation_id=? OR receipt_id=?",
                (observation.observation_id, receipt.receipt_id),
            ).fetchone()
            if row is not None:
                if row[0] != observation.observation_id or row[1] != receipt.receipt_id:
                    raise ValueError("receipt or observation identity is already bound")
                if row[2] != receipt_payload or row[3] != observation_payload:
                    raise ValueError("conflicting verifier receipt replay")
                return _observation(json.loads(row[3]))
            authorization = getattr(receipt, "authorization", None)
            if not callable(getattr(authorization, "matches", None)) or not authorization.matches(
                receipt, observation
            ):
                raise PermissionError(
                    "first verifier observation insert requires host producer authorization"
                )
            try:
                self._connection.execute(
                    "INSERT INTO verifier_learning_observations"
                    "(observation_id, receipt_id, receipt_json, observation_json) VALUES (?,?,?,?)",
                    (observation.observation_id, receipt.receipt_id, receipt_payload, observation_payload),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("conflicting verifier receipt replay") from exc
        return observation

    def get(self, observation_id: str) -> tuple[VerifierReceipt, LearningObservation] | None:
        row = self._connection.execute(
            "SELECT receipt_json, observation_json FROM verifier_learning_observations WHERE observation_id=?",
            (observation_id,),
        ).fetchone()
        if row is None:
            return None
        return VerifierReceipt(**json.loads(row[0])), _observation(json.loads(row[1]))

    def list(self, *, limit: int = 256) -> tuple[LearningObservation, ...]:
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        rows = self._connection.execute(
            "SELECT observation_json FROM verifier_learning_observations ORDER BY rowid LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(_observation(json.loads(row[0])) for row in rows)


__all__ = ["SQLiteVerifierObservationRepository"]
