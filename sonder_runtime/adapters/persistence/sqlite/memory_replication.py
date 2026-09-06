"""SQLite journal adapter for authoritative memory mutation transfer.

This adapter persists a bounded ordered journal and exposes the latest
authoritative records for rebuilding derived recall indexes.  It is deliberately
provider-neutral: a replicated database or transport must supply ownership,
durability, and network delivery guarantees around this journal.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import json
import sqlite3
from pathlib import Path

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect

from sonder_runtime.domain.memory.replication import (
    MemoryMutation,
    MemoryReplicationBatch,
    MemoryReplicationError,
)


_MAX_EXPORT_ROWS = 1024
_MAX_PRUNE_ROWS = 1024

_DDL = """
CREATE TABLE IF NOT EXISTS memory_replication_meta (
    source_id TEXT PRIMARY KEY,
    source_epoch INTEGER NOT NULL CHECK(source_epoch >= 1),
    next_sequence INTEGER NOT NULL CHECK(next_sequence >= 1),
    project_scope TEXT
);
CREATE TABLE IF NOT EXISTS memory_replication_log (
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
CREATE INDEX IF NOT EXISTS idx_memory_replication_entity
ON memory_replication_log(source_id, project, entity_kind, entity_id, version DESC);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteMemoryReplicationJournal:
    """Bounded SQLite journal for one source identity and optional project."""

    def __init__(self, path: str | Path = ":memory:", *, source_id: str = "cluster-a", project_scope: str | None = None) -> None:
        self.path = str(path)
        self.source_id = source_id
        self.project_scope = project_scope
        self._memory_connection = None
        # Validate identities and scope before creating any state.
        MemoryMutation(
            source_id=source_id, source_epoch=1, sequence=1,
            entity_kind="fact", entity_id="validation", version=1,
            operation="delete", project=project_scope or "global",
            payload={}, recorded_at=_utc_now(),
        )
        if project_scope is not None and project_scope == "":
            raise MemoryReplicationError("project scope must not be empty")
        with self._session() as connection:
            connection.executescript(_DDL)
            row = connection.execute(
                "SELECT project_scope FROM memory_replication_meta WHERE source_id=?",
                (self.source_id,),
            ).fetchone()
            if row is not None and row[0] is not None and row[0] != self.project_scope:
                raise MemoryReplicationError("journal project scope conflicts with persisted scope")
            if row is None:
                connection.execute(
                    "INSERT INTO memory_replication_meta(source_id,source_epoch,next_sequence,project_scope) VALUES(?,?,?,?)",
                    (self.source_id, 1, 1, self.project_scope),
                )

    def _connect(self):
        # Use the explicit ownership factory so managed runtime children can
        # account for every journal handle.  A journal's in-memory mode needs
        # one keeper connection; reopening ``:memory:`` for each operation
        # would silently create a fresh empty database on every call.
        if self.path == ":memory:":
            if self._memory_connection is None:
                self._memory_connection = owned_sqlite_connect(
                    self.path, check_same_thread=False,
                )
            return self._memory_connection
        connection = owned_sqlite_connect(self.path)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def close(self) -> None:
        """Close the keeper used by the optional in-memory journal."""
        connection, self._memory_connection = self._memory_connection, None
        if connection is not None:
            connection.close()

    @contextmanager
    def _session(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            if connection is not self._memory_connection:
                connection.close()

    def _scope(self, project: str | None) -> str | None:
        if project is None:
            return self.project_scope
        if self.project_scope is None:
            raise MemoryReplicationError(
                "project-scoped export requires a journal project scope"
            )
        if not isinstance(project, str) or not project:
            raise MemoryReplicationError("project scope must be non-empty")
        if self.project_scope is not None and project != self.project_scope:
            raise MemoryReplicationError("project scope cannot be widened")
        # Let MemoryMutation validate the full project grammar.
        MemoryMutation(
            source_id=self.source_id, source_epoch=1, sequence=1,
            entity_kind="fact", entity_id="validation", version=1,
            operation="delete", project=project, payload={}, recorded_at=_utc_now(),
        )
        return project

    @staticmethod
    def _row_to_mutation(row) -> MemoryMutation:
        return MemoryMutation(
            source_id=row[0], source_epoch=row[1], sequence=row[2],
            entity_kind=row[3], entity_id=row[4], version=row[5],
            operation=row[6], project=row[7], payload=json.loads(row[8]),
            recorded_at=row[9],
        )

    @staticmethod
    def _insert(connection, mutation: MemoryMutation) -> int:
        cursor = connection.execute(
            "INSERT INTO memory_replication_log"
            "(source_id,source_epoch,sequence,entity_kind,entity_id,version,operation,project,payload_json,recorded_at,digest)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                mutation.source_id, mutation.source_epoch, mutation.sequence,
                mutation.entity_kind, mutation.entity_id, mutation.version,
                mutation.operation, mutation.project,
                json.dumps(dict(mutation.payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True),
                mutation.recorded_at, mutation.digest,
            ),
        )
        return cursor.rowcount

    def _meta(self, connection):
        row = connection.execute(
            "SELECT source_epoch,next_sequence,project_scope FROM memory_replication_meta WHERE source_id=?",
            (self.source_id,),
        ).fetchone()
        if row is None:
            raise MemoryReplicationError("journal metadata is missing")
        return row

    def advance_epoch(self, source_epoch: int) -> None:
        if isinstance(source_epoch, bool) or not isinstance(source_epoch, int) or source_epoch < 1:
            raise MemoryReplicationError("source epoch must be positive")
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._meta(connection)[0]
            if source_epoch <= current:
                raise MemoryReplicationError("source epoch must advance")
            connection.execute(
                "UPDATE memory_replication_meta SET source_epoch=? WHERE source_id=?",
                (source_epoch, self.source_id),
            )
            connection.commit()

    def append(self, records: tuple[MemoryMutation, ...]) -> int:
        if type(records) is not tuple or not 1 <= len(records) <= _MAX_EXPORT_ROWS:
            raise ValueError("records must be a bounded non-empty tuple")
        for record in records:
            if not isinstance(record, MemoryMutation) or record.source_id != self.source_id:
                raise MemoryReplicationError("journal records must belong to this source")
            if self.project_scope is not None and record.project != self.project_scope:
                raise MemoryReplicationError("project scope cannot be widened")
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            epoch, next_sequence, _scope = self._meta(connection)
            inserted = 0
            expected = next_sequence
            for record in records:
                if record.source_epoch != epoch:
                    raise MemoryReplicationError("record source epoch is stale or not admitted")
                if record.sequence != expected:
                    existing = connection.execute(
                        "SELECT digest FROM memory_replication_log WHERE source_id=? AND sequence=?",
                        (self.source_id, record.sequence),
                    ).fetchone()
                    if existing is None or existing[0] != record.digest:
                        raise MemoryReplicationError("journal sequence is not contiguous")
                    # Replaying an already committed prefix is idempotent;
                    # it must not advance the append cursor a second time.
                    continue
                latest = connection.execute(
                    "SELECT MAX(version) FROM memory_replication_log "
                    "WHERE source_id=? AND project=? AND entity_kind=? AND entity_id=?",
                    (record.source_id, record.project, record.entity_kind, record.entity_id),
                ).fetchone()[0]
                if latest is not None and record.version <= latest:
                    raise MemoryReplicationError("entity version must advance")
                try:
                    inserted += self._insert(connection, record)
                except sqlite3.IntegrityError:
                    existing = connection.execute(
                        "SELECT digest FROM memory_replication_log WHERE source_id=? AND sequence=?",
                        (self.source_id, record.sequence),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] != record.digest:
                            raise MemoryReplicationError("journal sequence conflicts with existing evidence")
                    else:
                        same_version = connection.execute(
                            "SELECT digest FROM memory_replication_log WHERE source_id=? AND project=? AND entity_kind=? AND entity_id=? AND version=?",
                            (record.source_id, record.project, record.entity_kind, record.entity_id, record.version),
                        ).fetchone()
                        if same_version is None or same_version[0] != record.digest:
                            raise MemoryReplicationError("journal entity version conflicts")
                    if existing is not None and existing[0] == record.digest:
                        # A duplicate that happens to be at the cursor is also
                        # safe to replay without changing the next sequence.
                        pass
                    elif existing is None:
                        raise MemoryReplicationError("journal entity version conflicts")
                    else:
                        raise MemoryReplicationError("journal sequence conflicts with existing evidence")
                expected += 1
            connection.execute(
                "UPDATE memory_replication_meta SET next_sequence=? WHERE source_id=?",
                (expected, self.source_id),
            )
            connection.commit()
            return inserted

    def export(self, *, after_sequence: int = 0, limit: int = 256, project: str | None = None) -> MemoryReplicationBatch:
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= _MAX_EXPORT_ROWS:
            raise ValueError(f"limit must be within 1..{_MAX_EXPORT_ROWS}")
        scope = self._scope(project)
        with self._session() as connection:
            epoch, _next_sequence, _persisted_scope = self._meta(connection)
            clauses = ["source_id=?", "sequence>?",]
            parameters: list[object] = [self.source_id, after_sequence]
            if scope is not None:
                clauses.append("project=?")
                parameters.append(scope)
            rows = connection.execute(
                "SELECT source_id,source_epoch,sequence,entity_kind,entity_id,version,operation,project,payload_json,recorded_at "
                "FROM memory_replication_log WHERE " + " AND ".join(clauses)
                + " ORDER BY sequence LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        records = tuple(self._row_to_mutation(row) for row in selected)
        next_sequence = records[-1].sequence if records else after_sequence
        return MemoryReplicationBatch(self.source_id, epoch, after_sequence, records, next_sequence, has_more)

    def apply(self, batch: MemoryReplicationBatch) -> int:
        if not isinstance(batch, MemoryReplicationBatch):
            raise TypeError("memory replication batch is required")
        if self.project_scope is not None:
            for record in batch.records:
                if record.project != self.project_scope:
                    raise MemoryReplicationError("project scope cannot be widened")
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT source_epoch,next_sequence FROM memory_replication_meta WHERE source_id=?",
                (batch.source_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO memory_replication_meta(source_id,source_epoch,next_sequence,project_scope) VALUES(?,?,?,?)",
                    (batch.source_id, batch.source_epoch, 1, self.project_scope),
                )
                current_epoch, expected = batch.source_epoch, 1
            else:
                current_epoch, expected = row
                if batch.source_epoch < current_epoch:
                    raise MemoryReplicationError("replication batch has a stale source epoch")
                if batch.source_epoch > current_epoch:
                    current_epoch = batch.source_epoch
                    connection.execute(
                        "UPDATE memory_replication_meta SET source_epoch=? WHERE source_id=?",
                        (current_epoch, batch.source_id),
                    )
            inserted = 0
            for record in batch.records:
                existing = connection.execute(
                    "SELECT digest FROM memory_replication_log WHERE source_id=? AND sequence=?",
                    (record.source_id, record.sequence),
                ).fetchone()
                if existing is not None:
                    if existing[0] != record.digest:
                        raise MemoryReplicationError("replication sequence conflicts with existing evidence")
                    expected = max(expected, record.sequence + 1)
                    continue
                if record.source_epoch != current_epoch:
                    raise MemoryReplicationError("record source epoch does not match batch")
                if record.sequence != expected:
                    raise MemoryReplicationError("replication batch has a sequence gap")
                latest = connection.execute(
                    "SELECT MAX(version) FROM memory_replication_log "
                    "WHERE source_id=? AND project=? AND entity_kind=? AND entity_id=?",
                    (record.source_id, record.project, record.entity_kind, record.entity_id),
                ).fetchone()[0]
                if latest is not None and record.version <= latest:
                    raise MemoryReplicationError("replication entity version must advance")
                try:
                    inserted += self._insert(connection, record)
                except sqlite3.IntegrityError as exc:
                    raise MemoryReplicationError("replication entity version conflicts") from exc
                expected += 1
            connection.execute(
                "UPDATE memory_replication_meta SET next_sequence=? WHERE source_id=?",
                (expected, batch.source_id),
            )
            connection.commit()
            return inserted

    def _latest(self, *, project: str | None = None, tombstones: bool = False, limit: int = 1024):
        if type(limit) is not int or not 1 <= limit <= _MAX_EXPORT_ROWS:
            raise ValueError(f"limit must be within 1..{_MAX_EXPORT_ROWS}")
        scope = self._scope(project)
        clauses = ["source_id=?"]
        parameters: list[object] = [self.source_id]
        if scope is not None:
            clauses.append("project=?")
            parameters.append(scope)
        # The latest version is selected before filtering tombstones.  An older
        # delete must never reappear after a newer upsert, and vice versa.
        query = (
            "SELECT source_id,source_epoch,sequence,entity_kind,entity_id,version,operation,project,payload_json,recorded_at "
            "FROM (SELECT source_id,source_epoch,sequence,entity_kind,entity_id,version,operation,project,payload_json,recorded_at, "
            "ROW_NUMBER() OVER (PARTITION BY project,entity_kind,entity_id ORDER BY version DESC,sequence DESC) AS row_number "
            "FROM memory_replication_log WHERE " + " AND ".join(clauses) + ") "
            "WHERE row_number=1 AND operation=? ORDER BY project,entity_kind,entity_id LIMIT ?"
        )
        parameters = [self.source_id]
        if scope is not None:
            parameters.append(scope)
        parameters.extend(("delete" if tombstones else "upsert", limit))
        with self._session() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return tuple(self._row_to_mutation(row) for row in rows)

    def current_records(self, *, project: str | None = None, limit: int = 1024):
        return self._latest(project=project, tombstones=False, limit=limit)

    def tombstones(self, *, project: str | None = None, limit: int = 1024):
        return self._latest(project=project, tombstones=True, limit=limit)

    def prune_before(self, sequence: int, *, retain_tombstones: bool = True, limit: int = _MAX_PRUNE_ROWS) -> int:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("sequence must be positive")
        if type(limit) is not int or not 1 <= limit <= _MAX_PRUNE_ROWS:
            raise ValueError(f"limit must be within 1..{_MAX_PRUNE_ROWS}")
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            protected = connection.execute(
                "SELECT project,entity_kind,entity_id,MAX(version),"
                "(SELECT operation FROM memory_replication_log latest "
                " WHERE latest.source_id=memory_replication_log.source_id "
                " AND latest.project=memory_replication_log.project "
                " AND latest.entity_kind=memory_replication_log.entity_kind "
                " AND latest.entity_id=memory_replication_log.entity_id "
                " ORDER BY latest.version DESC LIMIT 1) "
                "FROM memory_replication_log "
                "WHERE source_id=? GROUP BY project,entity_kind,entity_id",
                (self.source_id,),
            ).fetchall()
            predicates = []
            parameters: list[object] = [self.source_id, sequence]
            for project, kind, entity_id, version, operation in protected:
                if operation == "delete" and not retain_tombstones:
                    continue
                predicates.append("NOT (project=? AND entity_kind=? AND entity_id=? AND version=?)")
                parameters.extend((project, kind, entity_id, version))
            where = " AND ".join(predicates) if predicates else "1"
            if retain_tombstones:
                where += " AND operation='upsert'"
            rows = connection.execute(
                "SELECT source_id,sequence FROM memory_replication_log WHERE source_id=? AND sequence<? AND "
                + where + " ORDER BY sequence LIMIT ?",
                (*parameters, limit),
            ).fetchall()
            connection.executemany(
                "DELETE FROM memory_replication_log WHERE source_id=? AND sequence=?",
                rows,
            )
            connection.commit()
            return len(rows)


__all__ = ["SQLiteMemoryReplicationJournal"]
