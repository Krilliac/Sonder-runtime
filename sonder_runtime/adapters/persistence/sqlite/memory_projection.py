"""Replay authoritative memory mutations into a SQLite memory store.

The journal is the transfer log; this adapter is the bounded materializer for
the canonical SQLite tables and their derived recall inputs.  It deliberately
does not perform network replication, owner election, fencing, or conflict
resolution outside the version and sequence evidence carried by a batch.
"""
from __future__ import annotations

from array import array
from contextlib import contextmanager
import json
import math
import sqlite3
from typing import Iterator

from sonder_runtime.domain.memory import rules as memory_rules
from sonder_runtime.domain.memory.replication import (
    MemoryMutation,
    MemoryReplicationBatch,
    MemoryReplicationError,
)


_MAX_ROWS = 1024
_MAX_TEXT = 64 * 1024
_MAX_EMBEDDING = 16_384
_ALLOWED_OUTCOME_SOURCES = frozenset(memory_rules.OUTCOME_SOURCES)

_DDL = """
CREATE TABLE IF NOT EXISTS memory_projection_log (
    source_id TEXT NOT NULL,
    source_epoch INTEGER NOT NULL CHECK(source_epoch >= 1),
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    operation TEXT NOT NULL CHECK(operation IN ('upsert','delete')),
    project TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY(source_id, sequence),
    UNIQUE(source_id, project, entity_kind, entity_id, version)
);
CREATE INDEX IF NOT EXISTS idx_memory_projection_entity
ON memory_projection_log(source_id, project, entity_kind, entity_id, version DESC);
CREATE TABLE IF NOT EXISTS memory_projection_state (
    source_id TEXT NOT NULL,
    source_epoch INTEGER NOT NULL CHECK(source_epoch >= 1),
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    operation TEXT NOT NULL CHECK(operation IN ('upsert','delete')),
    project TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY(source_id, project, entity_kind, entity_id)
);
CREATE TABLE IF NOT EXISTS memory_projection_cursors (
    source_id TEXT PRIMARY KEY,
    source_epoch INTEGER NOT NULL CHECK(source_epoch >= 1),
    next_sequence INTEGER NOT NULL CHECK(next_sequence >= 1)
);
"""


class MemoryProjectionError(MemoryReplicationError):
    """A batch cannot be safely materialized into the target memory store."""


def _bounded_text(payload: dict, key: str, *, required: bool = True) -> str | None:
    value = payload.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
        raise MemoryProjectionError(f"payload.{key} must be a bounded non-empty string")
    return value


def _optional_text(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > _MAX_TEXT:
        raise MemoryProjectionError(f"payload.{key} must be a bounded string")
    return value


def _embedding(payload: dict, key: str) -> bytes | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_EMBEDDING:
        raise MemoryProjectionError(f"payload.{key} must be a bounded float list")
    values: list[float] = []
    for item in value:
        if isinstance(item, bool):
            raise MemoryProjectionError(f"payload.{key} contains a non-numeric value")
        try:
            numeric = float(item)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MemoryProjectionError(f"payload.{key} contains a non-numeric value") from exc
        if not math.isfinite(numeric):
            raise MemoryProjectionError(f"payload.{key} contains a non-finite value")
        values.append(numeric)
    if not any(values):
        raise MemoryProjectionError(f"payload.{key} must not be all zero")
    return array("f", values).tobytes()


def _allowed(payload: dict, fields: frozenset[str], kind: str) -> None:
    unknown = set(payload) - fields
    if unknown:
        raise MemoryProjectionError(
            f"{kind} payload has unsupported fields: {', '.join(sorted(unknown))}"
        )


def _payload(record: MemoryMutation) -> dict:
    payload = dict(record.payload)
    if record.operation == "delete":
        if payload:
            raise MemoryProjectionError("tombstone payload must be empty")
        return payload
    return payload


def _outcome_parts(record: MemoryMutation, payload: dict) -> tuple[str, str, float, str]:
    interaction_id = _bounded_text(payload, "interaction_id")
    signal = _bounded_text(payload, "signal")
    if signal not in memory_rules.VALID_SIGNALS:
        raise MemoryProjectionError("payload.signal is not a supported outcome")
    if record.entity_id != f"{interaction_id}:{signal}":
        raise MemoryProjectionError("outcome entity_id does not match its payload")
    source = _bounded_text(payload, "source")
    if source not in _ALLOWED_OUTCOME_SOURCES:
        raise MemoryProjectionError("payload.source is not a supported outcome source")
    reward = payload.get("reward")
    if isinstance(reward, bool):
        raise MemoryProjectionError("payload.reward must be finite")
    try:
        reward = float(reward)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MemoryProjectionError("payload.reward must be finite") from exc
    if not math.isfinite(reward) or reward != memory_rules.reward_score(signal):
        raise MemoryProjectionError("payload.reward does not match the outcome signal")
    return interaction_id, signal, reward, source


class SQLiteMemoryReplicationProjection:
    """Apply ordered authoritative records to one SQLite memory database."""

    def __init__(self, connection: sqlite3.Connection, *, project_scope: str | None = None) -> None:
        if not hasattr(connection, "execute"):
            raise TypeError("a SQLite connection is required")
        if project_scope is not None and (not isinstance(project_scope, str) or not project_scope):
            raise MemoryProjectionError("project_scope must be a non-empty string")
        self._conn = connection
        self.project_scope = project_scope
        self._conn.executescript(_DDL)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        savepoint = "sonder_memory_projection"
        nested = self._conn.in_transaction
        if nested:
            self._conn.execute(f"SAVEPOINT {savepoint}")
        else:
            self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            if nested:
                self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self._conn.rollback()
            raise
        else:
            if nested:
                self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self._conn.commit()

    def _check_scope(self, record: MemoryMutation) -> None:
        if self.project_scope is not None and record.project != self.project_scope:
            raise MemoryProjectionError("project scope cannot be widened")

    def apply(self, batch: MemoryReplicationBatch) -> int:
        """Apply one ordered page, returning newly materialized record count."""
        if not isinstance(batch, MemoryReplicationBatch):
            raise TypeError("memory replication batch is required")
        if len(batch.records) > _MAX_ROWS:
            raise MemoryProjectionError("replication batch exceeds the projection bound")
        for record in batch.records:
            self._check_scope(record)

        inserted = 0
        with self._transaction():
            cursor = self._conn.execute(
                "SELECT source_epoch,next_sequence FROM memory_projection_cursors WHERE source_id=?",
                (batch.source_id,),
            ).fetchone()
            if cursor is None:
                current_epoch, expected = batch.source_epoch, 1
                self._conn.execute(
                    "INSERT INTO memory_projection_cursors(source_id,source_epoch,next_sequence) VALUES(?,?,?)",
                    (batch.source_id, current_epoch, expected),
                )
            else:
                current_epoch, expected = int(cursor[0]), int(cursor[1])
                if batch.source_epoch < current_epoch:
                    raise MemoryProjectionError("replication batch has a stale source epoch")
                if batch.source_epoch > current_epoch:
                    current_epoch = batch.source_epoch

            for record in batch.records:
                existing = self._conn.execute(
                    "SELECT digest FROM memory_projection_log WHERE source_id=? AND sequence=?",
                    (record.source_id, record.sequence),
                ).fetchone()
                if existing is not None:
                    if existing[0] != record.digest:
                        raise MemoryProjectionError("replication sequence conflicts with existing evidence")
                    expected = max(expected, record.sequence + 1)
                    continue
                if record.source_epoch != current_epoch:
                    raise MemoryProjectionError("record source epoch does not match the target cursor")
                if record.sequence != expected:
                    raise MemoryProjectionError("replication batch has a sequence gap")
                latest = self._conn.execute(
                    "SELECT version FROM memory_projection_state WHERE source_id=? AND project=? "
                    "AND entity_kind=? AND entity_id=?",
                    (record.source_id, record.project, record.entity_kind, record.entity_id),
                ).fetchone()
                if latest is not None and record.version <= int(latest[0]):
                    raise MemoryProjectionError("replication entity version must advance")
                payload = _payload(record)
                self._validate_record(record, payload)
                self._conn.execute(
                    "INSERT INTO memory_projection_log"
                    "(source_id,source_epoch,sequence,entity_kind,entity_id,version,operation,project,payload_json,recorded_at,digest) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.source_id, record.source_epoch, record.sequence,
                        record.entity_kind, record.entity_id, record.version,
                        record.operation, record.project,
                        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
                        record.recorded_at, record.digest,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO memory_projection_state"
                    "(source_id,source_epoch,sequence,entity_kind,entity_id,version,operation,project,payload_json,recorded_at,digest) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(source_id,project,entity_kind,entity_id) DO UPDATE SET "
                    "source_epoch=excluded.source_epoch,sequence=excluded.sequence,version=excluded.version,"
                    "operation=excluded.operation,payload_json=excluded.payload_json,recorded_at=excluded.recorded_at,digest=excluded.digest",
                    (
                        record.source_id, record.source_epoch, record.sequence,
                        record.entity_kind, record.entity_id, record.version,
                        record.operation, record.project,
                        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
                        record.recorded_at, record.digest,
                    ),
                )
                self._materialize(record, payload)
                expected = record.sequence + 1
                inserted += 1

            self._conn.execute(
                "UPDATE memory_projection_cursors SET source_epoch=?,next_sequence=? WHERE source_id=?",
                (current_epoch, expected, batch.source_id),
            )
        return inserted

    def _validate_record(self, record: MemoryMutation, payload: dict) -> None:
        if record.is_tombstone:
            if record.entity_kind == "outcome":
                try:
                    _interaction_id, signal = record.entity_id.rsplit(":", 1)
                except ValueError as exc:
                    raise MemoryProjectionError("outcome tombstone identity is malformed") from exc
                if not _interaction_id or signal not in memory_rules.VALID_SIGNALS:
                    raise MemoryProjectionError("outcome tombstone identity is malformed")
            return
        if record.entity_kind == "fact":
            _allowed(payload, frozenset({"text", "embedding"}), "fact")
            _bounded_text(payload, "text")
            _embedding(payload, "embedding")
        elif record.entity_kind == "interaction":
            _allowed(
                payload,
                frozenset({"task", "retrieved_ctx", "response", "tier", "session_id", "task_embedding", "task_embedding_model", "task_embedding_revision", "task_embedding_dim", "tokens_in", "tokens_out", "token_source"}),
                "interaction",
            )
            _bounded_text(payload, "task")
            _optional_text(payload, "retrieved_ctx")
            _optional_text(payload, "response")
            _bounded_text(payload, "tier")
            _optional_text(payload, "session_id")
            _embedding(payload, "task_embedding")
            _optional_text(payload, "task_embedding_model")
            _optional_text(payload, "task_embedding_revision")
            if "task_embedding_dim" in payload and payload["task_embedding_dim"] is not None:
                value = payload["task_embedding_dim"]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise MemoryProjectionError("payload.task_embedding_dim must be positive")
            for key in ("tokens_in", "tokens_out"):
                if key in payload and payload[key] is not None:
                    value = payload[key]
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise MemoryProjectionError(f"payload.{key} must be non-negative")
            _optional_text(payload, "token_source")
        elif record.entity_kind == "outcome":
            _allowed(payload, frozenset({"interaction_id", "signal", "reward", "source"}), "outcome")
            _outcome_parts(record, payload)
        elif record.entity_kind == "preference":
            _allowed(payload, frozenset({"key", "text", "confidence", "evidence_count", "enabled", "source_interaction"}), "preference")
            _bounded_text(payload, "key")
            _bounded_text(payload, "text")
            confidence = payload.get("confidence", 0.5)
            if isinstance(confidence, bool):
                raise MemoryProjectionError("payload.confidence must be within 0..1")
            try:
                confidence = float(confidence)
            except (TypeError, ValueError, OverflowError) as exc:
                raise MemoryProjectionError("payload.confidence must be within 0..1") from exc
            if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                raise MemoryProjectionError("payload.confidence must be within 0..1")
            evidence_count = payload.get("evidence_count", 1)
            if isinstance(evidence_count, bool) or not isinstance(evidence_count, int) or evidence_count < 1:
                raise MemoryProjectionError("payload.evidence_count must be positive")
            if "enabled" in payload and not isinstance(payload["enabled"], bool):
                raise MemoryProjectionError("payload.enabled must be boolean")
            _optional_text(payload, "source_interaction")
        elif record.entity_kind == "lesson_decision":
            # Governance records remain authoritative in the projection state;
            # the existing lesson tables have richer lifecycle invariants and
            # are intentionally rebuilt by their own application service.
            _allowed(payload, frozenset({"decision", "reason", "source_interaction"}), "lesson_decision")
            _bounded_text(payload, "decision")
            _optional_text(payload, "reason")
            _optional_text(payload, "source_interaction")

    def _materialize(self, record: MemoryMutation, payload: dict) -> None:
        if record.entity_kind == "fact":
            self._materialize_fact(record, payload)
        elif record.entity_kind == "interaction":
            self._materialize_interaction(record, payload)
        elif record.entity_kind == "outcome":
            self._materialize_outcome(record, payload)
        elif record.entity_kind == "preference":
            self._materialize_preference(record, payload)

    def _materialize_fact(self, record: MemoryMutation, payload: dict) -> None:
        existing = self._conn.execute("SELECT project FROM facts WHERE id=?", (record.entity_id,)).fetchone()
        if existing is not None and existing[0] != record.project:
            raise MemoryProjectionError("fact identity is already bound to another project")
        if record.is_tombstone:
            self._conn.execute("DELETE FROM facts WHERE id=? AND project=?", (record.entity_id, record.project))
            return
        self._conn.execute(
            "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET project=excluded.project,text=excluded.text,embedding=excluded.embedding",
            (record.entity_id, record.project, payload["text"], _embedding(payload, "embedding")),
        )

    def _materialize_interaction(self, record: MemoryMutation, payload: dict) -> None:
        existing = self._conn.execute("SELECT project FROM interactions WHERE id=?", (record.entity_id,)).fetchone()
        if existing is not None and existing[0] not in (None, record.project):
            raise MemoryProjectionError("interaction identity is already bound to another project")
        if record.is_tombstone:
            self._conn.execute("DELETE FROM outcomes WHERE interaction_id=?", (record.entity_id,))
            self._conn.execute("DELETE FROM lesson_usage WHERE interaction_id=?", (record.entity_id,))
            self._conn.execute("DELETE FROM lesson_distillations WHERE interaction_id=?", (record.entity_id,))
            self._conn.execute("DELETE FROM interactions WHERE id=? AND (project=? OR project IS NULL)", (record.entity_id, record.project))
            return
        self._conn.execute(
            "INSERT INTO interactions(id,task,retrieved_ctx,response,tier,session_id,task_embedding,tokens_in,tokens_out,token_source,project,project_explicit,task_embedding_model,task_embedding_revision,task_embedding_dim) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET task=excluded.task,retrieved_ctx=excluded.retrieved_ctx,response=excluded.response,tier=excluded.tier,session_id=excluded.session_id,task_embedding=excluded.task_embedding,tokens_in=excluded.tokens_in,tokens_out=excluded.tokens_out,token_source=excluded.token_source,project=excluded.project,project_explicit=excluded.project_explicit,task_embedding_model=excluded.task_embedding_model,task_embedding_revision=excluded.task_embedding_revision,task_embedding_dim=excluded.task_embedding_dim",
            (
                record.entity_id, payload["task"], payload.get("retrieved_ctx", ""), payload.get("response", ""),
                payload["tier"], payload.get("session_id"), _embedding(payload, "task_embedding"),
                payload.get("tokens_in"), payload.get("tokens_out"), payload.get("token_source"),
                record.project, 1, payload.get("task_embedding_model"), payload.get("task_embedding_revision"), payload.get("task_embedding_dim"),
            ),
        )

    def _materialize_outcome(self, record: MemoryMutation, payload: dict) -> None:
        if record.is_tombstone:
            interaction_id, signal = record.entity_id.rsplit(":", 1)
            self._conn.execute("DELETE FROM outcomes WHERE interaction_id=? AND signal=?", (interaction_id, signal))
            return
        interaction_id, signal, reward, source = _outcome_parts(record, payload)
        if self._conn.execute("SELECT 1 FROM interactions WHERE id=?", (interaction_id,)).fetchone() is None:
            raise MemoryProjectionError("outcome references an interaction not yet materialized")
        existing = self._conn.execute(
            "SELECT reward,source FROM outcomes WHERE interaction_id=? AND signal=?",
            (interaction_id, signal),
        ).fetchone()
        if existing is None:
            self._conn.execute(
                "INSERT INTO outcomes(interaction_id,signal,reward,source) VALUES(?,?,?,?)",
                (interaction_id, signal, reward, source),
            )
        elif tuple(existing) != (reward, source):
            raise MemoryProjectionError("outcome evidence conflicts with the target store")

    def _materialize_preference(self, record: MemoryMutation, payload: dict) -> None:
        existing = self._conn.execute("SELECT scope FROM preferences WHERE id=?", (record.entity_id,)).fetchone()
        if existing is not None and existing[0] != record.project:
            raise MemoryProjectionError("preference identity is already bound to another scope")
        if record.is_tombstone:
            self._conn.execute("DELETE FROM preferences WHERE id=? AND scope=?", (record.entity_id, record.project))
            return
        conflict = self._conn.execute(
            "SELECT id FROM preferences WHERE scope=? AND key=? AND id<>?",
            (record.project, payload["key"], record.entity_id),
        ).fetchone()
        if conflict is not None:
            raise MemoryProjectionError("preference scope/key conflicts with another identity")
        self._conn.execute(
            "INSERT INTO preferences(id,scope,key,text,source_interaction,confidence,evidence_count,enabled) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET scope=excluded.scope,key=excluded.key,text=excluded.text,source_interaction=excluded.source_interaction,confidence=excluded.confidence,evidence_count=excluded.evidence_count,enabled=excluded.enabled,revision=preferences.revision+1,updated_ts=CURRENT_TIMESTAMP",
            (
                record.entity_id, record.project, payload["key"], payload["text"], payload.get("source_interaction"),
                float(payload.get("confidence", 0.5)), int(payload.get("evidence_count", 1)),
                1 if payload.get("enabled", True) else 0,
            ),
        )

    def rebuild(self, *, source_id: str, project: str | None = None, limit: int = _MAX_ROWS) -> int:
        """Re-materialize latest state after restoring a fresh derived store."""
        if not isinstance(source_id, str) or not source_id:
            raise MemoryProjectionError("source_id is required")
        if type(limit) is not int or not 1 <= limit <= _MAX_ROWS:
            raise ValueError(f"limit must be within 1..{_MAX_ROWS}")
        if self.project_scope is not None and project not in (None, self.project_scope):
            raise MemoryProjectionError("project scope cannot be widened")
        clauses = ["source_id=?"]
        params: list[object] = [source_id]
        if project is not None:
            clauses.append("project=?")
            params.append(project)
        rows = self._conn.execute(
            "SELECT source_id,source_epoch,sequence,entity_kind,entity_id,version,operation,project,payload_json,recorded_at "
            "FROM memory_projection_state WHERE " + " AND ".join(clauses) + " ORDER BY sequence LIMIT ?",
            (*params, limit),
        ).fetchall()
        rebuilt = 0
        with self._transaction():
            for row in rows:
                record = MemoryMutation(
                    source_id=row[0], source_epoch=row[1], sequence=row[2], entity_kind=row[3], entity_id=row[4],
                    version=row[5], operation=row[6], project=row[7], payload=json.loads(row[8]), recorded_at=row[9],
                )
                self._validate_record(record, dict(record.payload))
                self._materialize(record, dict(record.payload))
                rebuilt += 1
        return rebuilt

    def _records(self, *, source_id: str, project: str | None, operation: str | None, limit: int) -> tuple[MemoryMutation, ...]:
        if type(limit) is not int or not 1 <= limit <= _MAX_ROWS:
            raise ValueError(f"limit must be within 1..{_MAX_ROWS}")
        if self.project_scope is not None and project not in (None, self.project_scope):
            raise MemoryProjectionError("project scope cannot be widened")
        clauses = ["source_id=?"]
        params: list[object] = [source_id]
        if project is not None:
            clauses.append("project=?")
            params.append(project)
        if operation is not None:
            clauses.append("operation=?")
            params.append(operation)
        rows = self._conn.execute(
            "SELECT source_id,source_epoch,sequence,entity_kind,entity_id,version,operation,project,payload_json,recorded_at "
            "FROM memory_projection_state WHERE " + " AND ".join(clauses) + " ORDER BY sequence LIMIT ?",
            (*params, limit),
        ).fetchall()
        return tuple(
            MemoryMutation(
                source_id=row[0], source_epoch=row[1], sequence=row[2], entity_kind=row[3], entity_id=row[4],
                version=row[5], operation=row[6], project=row[7], payload=json.loads(row[8]), recorded_at=row[9],
            )
            for row in rows
        )

    def current_records(self, *, source_id: str, project: str | None = None, limit: int = _MAX_ROWS) -> tuple[MemoryMutation, ...]:
        return self._records(source_id=source_id, project=project, operation="upsert", limit=limit)

    def tombstones(self, *, source_id: str, project: str | None = None, limit: int = _MAX_ROWS) -> tuple[MemoryMutation, ...]:
        return self._records(source_id=source_id, project=project, operation="delete", limit=limit)


__all__ = ["MemoryProjectionError", "SQLiteMemoryReplicationProjection"]
