"""Append-only SQLite persistence for host-authenticated verifier observations."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from ....application.memory.learning_ladder import LearningObservation
from ....application.memory.receipt_observation import (
    VerifierReceipt,
    _ObservationAuthorization,
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
# Subject-scoped reads use this expression index so promotion never loads
# unrelated projects or subjects under its write snapshot.
_SUBJECT_INDEX_DDL = """CREATE INDEX IF NOT EXISTS verifier_learning_observations_subject
ON verifier_learning_observations(
    json_extract(receipt_json, '$.project_scope'),
    json_extract(receipt_json, '$.workspace_scope'),
    json_extract(observation_json, '$.content')
);"""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _observation(value: dict) -> LearningObservation:
    from datetime import datetime
    if not isinstance(value, dict):
        raise ValueError("corrupt verifier observation payload")
    try:
        copied = dict(value)
        copied["observed_at"] = datetime.fromisoformat(copied["observed_at"])
        copied["provenance"] = tuple(copied["provenance"])
        return LearningObservation(**copied)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("corrupt verifier observation payload") from exc


def _decode_pair(receipt_raw: str, observation_raw: str) -> tuple[VerifierReceipt, LearningObservation]:
    try:
        receipt_data = json.loads(receipt_raw)
        observation_data = json.loads(observation_raw)
        if not isinstance(receipt_data, dict) or not isinstance(observation_data, dict):
            raise ValueError("corrupt verifier observation JSON")
        return VerifierReceipt(**receipt_data), _observation(observation_data)
    except (TypeError, ValueError) as exc:
        raise ValueError("corrupt verifier observation JSON") from exc


class SQLiteVerifierObservationRepository:
    """Persist and replay receipt/observation pairs on one caller-owned connection."""

    _SUBJECT_QUERY = (
        "SELECT receipt_json, observation_json FROM verifier_learning_observations "
        "WHERE json_extract(receipt_json, '$.project_scope')=? "
        "AND json_extract(receipt_json, '$.workspace_scope')=? "
        "AND json_extract(observation_json, '$.content')=? "
        "ORDER BY rowid LIMIT ?"
    )

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        for statement in (
            _TABLE_DDL, _UPDATE_TRIGGER_DDL, _DELETE_TRIGGER_DDL, _SUBJECT_INDEX_DDL,
        ):
            self._connection.execute(statement)

    _NEGATIVE_QUERY = (
        "SELECT receipt_json, observation_json FROM verifier_learning_observations "
        "WHERE json_extract(receipt_json, '$.project_scope')=? "
        "AND json_extract(receipt_json, '$.workspace_scope')=? "
        "AND json_extract(observation_json, '$.content')=? "
        "AND json_extract(receipt_json, '$.verifier_outcome')='failed' "
        "AND json_extract(observation_json, '$.positive')=0 "
        "ORDER BY rowid LIMIT ?"
    )

    def list_subject_negative_pairs(
        self, project_scope: str, content: str, *, limit: int = 16,
    ) -> tuple[tuple[VerifierReceipt, LearningObservation], ...]:
        """Read verified-negative rows for one subject regardless of volume."""
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError("limit must be between 1 and 256")
        rows = self._connection.execute(
            self._NEGATIVE_QUERY, (project_scope, project_scope, content, limit)
        ).fetchall()
        return tuple(_decode_pair(row[0], row[1]) for row in rows)

    def list_subject_pairs(
        self, project_scope: str, content: str, *, limit: int = 10_001,
    ) -> tuple[tuple[VerifierReceipt, LearningObservation], ...]:
        """Read one project's rows for one exact subject token (indexed)."""
        if type(limit) is not int or not 1 <= limit <= 10_001:
            raise ValueError("limit must be between 1 and 10001")
        rows = self._connection.execute(
            self._SUBJECT_QUERY, (project_scope, project_scope, content, limit)
        ).fetchall()
        return tuple(_decode_pair(row[0], row[1]) for row in rows)

    @contextmanager
    def _transaction(self):
        nested = bool(self._connection.in_transaction)
        if nested:
            self._connection.execute("SAVEPOINT sonder_verifier_observation_write")
        else:
            self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
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
            # Exact private type first: a duck-typed stand-in whose matches()
            # returns True must never reach the insert.
            if (
                type(receipt) is not VerifierReceipt
                or type(observation) is not LearningObservation
                or type(authorization) is not _ObservationAuthorization
                or not authorization.matches(receipt, observation)
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
        return _decode_pair(row[0], row[1])

    def get_many(
        self, observation_ids: tuple[str, ...] | list[str],
    ) -> dict[str, tuple[VerifierReceipt, LearningObservation]]:
        """Batch-load verifier observations in bounded SQL chunks."""
        unique = tuple(dict.fromkeys(oid for oid in observation_ids if oid))
        result: dict[str, tuple[VerifierReceipt, LearningObservation]] = {}
        chunk_size = 500
        for start in range(0, len(unique), chunk_size):
            chunk = unique[start:start + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._connection.execute(
                "SELECT observation_id, receipt_json, observation_json "
                f"FROM verifier_learning_observations WHERE observation_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for obs_id, receipt_raw, obs_raw in rows:
                result[obs_id] = _decode_pair(receipt_raw, obs_raw)
        return result

    def list_pairs(self, *, limit: int = 10_001) -> tuple[tuple[VerifierReceipt, LearningObservation], ...]:
        """Read a bounded complete snapshot, or let callers fail closed."""
        if type(limit) is not int or not 1 <= limit <= 10_001:
            raise ValueError("limit must be between 1 and 10001")
        rows = self._connection.execute(
            "SELECT receipt_json, observation_json "
            "FROM verifier_learning_observations ORDER BY rowid LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(_decode_pair(row[0], row[1]) for row in rows)

    def list(self, *, limit: int = 256) -> tuple[LearningObservation, ...]:
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        rows = self._connection.execute(
            "SELECT observation_json FROM verifier_learning_observations ORDER BY rowid LIMIT ?",
            (limit,),
        ).fetchall()
        decoded = []
        for row in rows:
            try:
                obs_data = json.loads(row[0])
            except (TypeError, ValueError) as exc:
                raise ValueError("corrupt verifier observation JSON") from exc
            decoded.append(_observation(obs_data))
        return tuple(decoded)


__all__ = ["SQLiteVerifierObservationRepository"]
