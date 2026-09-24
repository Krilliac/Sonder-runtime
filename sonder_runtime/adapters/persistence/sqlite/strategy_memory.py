"""Content-free strategy experience index inside the canonical memory database."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from hashlib import sha256

from sonder_runtime.application.memory.strategy_memory import StrategyExperience
from sonder_runtime.domain.strategy.models import StrategyUsage

_DDL = """
CREATE TABLE IF NOT EXISTS strategy_experience (
    experience_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    project_digest TEXT NOT NULL,
    attempt_digest TEXT NOT NULL,
    signature_digest TEXT NOT NULL,
    objective_digest TEXT NOT NULL,
    family TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('succeeded','failed')),
    failure_class TEXT NOT NULL,
    failure_fingerprint TEXT NOT NULL,
    verifier_fingerprint TEXT NOT NULL,
    language TEXT NOT NULL,
    subsystem_digest TEXT NOT NULL,
    progress TEXT NOT NULL,
    usage_json TEXT NOT NULL,
    evidence_digests_json TEXT NOT NULL,
    verifier_observation_id TEXT NOT NULL,
    UNIQUE(run_id,attempt_id)
);
CREATE INDEX IF NOT EXISTS ix_strategy_experience_retrieval
ON strategy_experience(project_digest, failure_class, family, verifier_fingerprint);
CREATE INDEX IF NOT EXISTS ix_strategy_experience_signature
ON strategy_experience(project_digest, signature_digest);
CREATE TABLE IF NOT EXISTS strategy_memory_selection (
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    experience_id TEXT NOT NULL REFERENCES strategy_experience(experience_id),
    position INTEGER NOT NULL,
    outcome TEXT CHECK(outcome IN ('succeeded','failed') OR outcome IS NULL),
    PRIMARY KEY(run_id,attempt_id,experience_id),
    UNIQUE(run_id,attempt_id,position)
);
CREATE INDEX IF NOT EXISTS ix_strategy_memory_failed_reuse
ON strategy_memory_selection(experience_id,outcome);
CREATE TRIGGER IF NOT EXISTS strategy_experience_immutable_update
BEFORE UPDATE ON strategy_experience BEGIN
    SELECT RAISE(ABORT, 'strategy experiences are immutable');
END;
CREATE TRIGGER IF NOT EXISTS strategy_experience_immutable_delete
BEFORE DELETE ON strategy_experience BEGIN
    SELECT RAISE(ABORT, 'strategy experiences are immutable');
END;
"""
_FIELDS = tuple(StrategyExperience.__dataclass_fields__)
_COLUMN_NAMES = ",".join("usage_json" if x == "usage" else "evidence_digests_json"
                         if x == "evidence_digests" else x for x in _FIELDS)


def _payload(row: StrategyExperience) -> tuple:
    return tuple(
        json.dumps(asdict(row.usage), sort_keys=True, separators=(",", ":")) if key == "usage"
        else json.dumps(row.evidence_digests, separators=(",", ":")) if key == "evidence_digests"
        else getattr(row, key) for key in _FIELDS
    )


def _experience(row) -> StrategyExperience:
    values = dict(zip(_FIELDS, row))
    values["usage"] = StrategyUsage(**json.loads(values["usage"]))
    values["evidence_digests"] = tuple(json.loads(values["evidence_digests"]))
    return StrategyExperience(**values)


def _identity(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("bounded run and attempt identities required")
    return sha256(json.dumps(value, ensure_ascii=True).encode("ascii")).hexdigest()


class SQLiteStrategyExperienceRepository:
    """Atomic, immutable attempt projection and explicit reuse attribution."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        statement = ""
        for line in _DDL.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                self._connection.execute(statement)
                statement = ""

    def append(self, experience: StrategyExperience) -> StrategyExperience:
        if not isinstance(experience, StrategyExperience):
            raise TypeError("typed strategy experience is required")
        old = self.get(experience.experience_id)
        if old is not None:
            if old != experience:
                raise ValueError("conflicting strategy experience identity")
            return old
        prior_project = self._connection.execute(
            "SELECT project_digest FROM strategy_experience WHERE run_id=? LIMIT 1",
            (experience.run_id,),
        ).fetchone()
        if prior_project is not None and prior_project[0] != experience.project_digest:
            raise ValueError("strategy run is already bound to another project")
        try:
            self._connection.execute(
                f"INSERT INTO strategy_experience({_COLUMN_NAMES}) VALUES ({','.join('?' for _ in _FIELDS)})",
                _payload(experience),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("strategy attempt identity is already bound") from exc
        return experience

    def get(self, experience_id: str) -> StrategyExperience | None:
        row = self._connection.execute(
            f"SELECT {_COLUMN_NAMES} FROM strategy_experience WHERE experience_id=?",
            (experience_id,),
        ).fetchone()
        return None if row is None else _experience(row)

    def relevant(self, project_digest: str, *, failure_class: str, family: str,
                 verifier_fingerprint: str, language: str, subsystem_digest: str, outcome: str,
                 limit: int) -> tuple[StrategyExperience, ...]:
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError("strategy lookup is out of bounds")
        conditions = ["project_digest=?"]
        values = [project_digest]
        for name, requested in (
            ("failure_class", failure_class), ("family", family),
            ("verifier_fingerprint", verifier_fingerprint),
            ("language", language), ("subsystem_digest", subsystem_digest),
            ("outcome", outcome),
        ):
            if requested:
                # Older sealed attempts had no trusted language metadata and
                # were indexed as unknown. Include them for compatible scoped
                # retrieval, but expose their unknown status in each ref.
                if name == "language" and requested != "unknown":
                    conditions.append("language IN (?, 'unknown')")
                else:
                    conditions.append(name + "=?")
                values.append(requested)
        rows = self._connection.execute(
            f"SELECT {_COLUMN_NAMES} FROM strategy_experience "
            f"WHERE {' AND '.join(conditions)} ORDER BY rowid DESC LIMIT ?",
            (*values, limit),
        ).fetchall()
        return tuple(_experience(row) for row in rows)

    def by_signature(self, project_digest: str, signature_digest: str,
                     *, limit: int) -> tuple[StrategyExperience, ...]:
        if type(limit) is not int or not 1 <= limit <= 10_001:
            raise ValueError("strategy evidence snapshot is out of bounds")
        rows = self._connection.execute(
            f"SELECT {_COLUMN_NAMES} FROM strategy_experience "
            "WHERE project_digest=? AND signature_digest=? ORDER BY rowid LIMIT ?",
            (project_digest, signature_digest, limit),
        ).fetchall()
        return tuple(_experience(row) for row in rows)

    def selections(self, run_id: str, attempt_id: str) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT experience_id FROM strategy_memory_selection "
            "WHERE run_id=? AND attempt_id=? ORDER BY position",
            (_identity(run_id), _identity(attempt_id)),
        ).fetchall()
        return tuple(row[0] for row in rows)

    def select(self, run_id: str, attempt_id: str, experience_ids: tuple[str, ...]) -> None:
        if not 1 <= len(experience_ids) <= 32 or len(set(experience_ids)) != len(experience_ids):
            raise ValueError("bounded distinct strategy selection required")
        old = self.selections(run_id, attempt_id)
        if old:
            if old != experience_ids:
                raise ValueError("strategy selection changed after publication")
            return
        try:
            self._connection.executemany(
                "INSERT INTO strategy_memory_selection"
                "(run_id,attempt_id,experience_id,position,outcome) VALUES(?,?,?,?,NULL)",
                ((_identity(run_id), _identity(attempt_id), identity, position)
                 for position, identity in enumerate(experience_ids)),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("strategy selection changed during publication") from exc

    def complete(self, run_id: str, attempt_id: str, outcome: str) -> None:
        if outcome not in {"succeeded", "failed"}:
            raise ValueError("terminal reuse outcome is required")
        conflicting = self._connection.execute(
            "SELECT 1 FROM strategy_memory_selection WHERE run_id=? AND attempt_id=? "
            "AND outcome IS NOT NULL AND outcome!=? LIMIT 1",
            (_identity(run_id), _identity(attempt_id), outcome),
        ).fetchone()
        if conflicting:
            raise ValueError("strategy reuse outcome changed")
        self._connection.execute(
            "UPDATE strategy_memory_selection SET outcome=? "
            "WHERE run_id=? AND attempt_id=? AND outcome IS NULL",
            (outcome, _identity(run_id), _identity(attempt_id)),
        )

    def failed_reuses(self, experience_id: str) -> int:
        return int(self._connection.execute(
            "SELECT count(*) FROM strategy_memory_selection "
            "WHERE experience_id=? AND outcome='failed'", (experience_id,),
        ).fetchone()[0])


__all__ = ["SQLiteStrategyExperienceRepository"]
