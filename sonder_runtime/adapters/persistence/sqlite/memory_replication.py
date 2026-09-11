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
    MemoryReplicaReceipt,
    MemoryReplicationBatch,
    MemoryReplicationError,
)
from sonder_runtime.adapters.persistence.sqlite.memory_projection import (
    SQLiteMemoryReplicationProjection,
)


_MAX_EXPORT_ROWS = 1024
_MAX_PRUNE_ROWS = 1024

MEMORY_REPLICATION_DDL = """
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


def _validate_source(source_id: object, project_scope: object) -> None:
    """Validate source metadata without creating a journal row."""
    MemoryMutation(
        source_id=source_id,
        source_epoch=1,
        sequence=1,
        entity_kind="fact",
        entity_id="validation",
        version=1,
        operation="delete",
        project=project_scope or "global",
        payload={},
        recorded_at=_utc_now(),
    )
    if project_scope is not None and project_scope == "":
        raise MemoryReplicationError("project scope must not be empty")


def ensure_memory_replication_source(
    connection,
    *,
    source_id: str,
    project_scope: str | None,
) -> tuple[int, int, str | None]:
    """Return one durable source cursor inside the caller's transaction.

    The caller owns both the SQLite transaction and the schema.  This narrow
    helper is shared by the standalone journal and an authoritative write-side
    adapter, so a materialized row and its mutation record can share one
    database commit.
    """
    _validate_source(source_id, project_scope)
    row = connection.execute(
        "SELECT source_epoch,next_sequence,project_scope "
        "FROM memory_replication_meta WHERE source_id=?",
        (source_id,),
    ).fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO memory_replication_meta"
            "(source_id,source_epoch,next_sequence,project_scope) "
            "VALUES(?,?,?,?)",
            (source_id, 1, 1, project_scope),
        )
        return 1, 1, project_scope
    if row[2] is not None and row[2] != project_scope:
        raise MemoryReplicationError(
            "journal project scope conflicts with persisted scope"
        )
    return int(row[0]), int(row[1]), row[2]


def _insert_mutation(connection, mutation: MemoryMutation) -> int:
    cursor = connection.execute(
        "INSERT INTO memory_replication_log"
        "(source_id,source_epoch,sequence,entity_kind,entity_id,version,operation,project,payload_json,recorded_at,digest)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            mutation.source_id, mutation.source_epoch, mutation.sequence,
            mutation.entity_kind, mutation.entity_id, mutation.version,
            mutation.operation, mutation.project,
            json.dumps(
                dict(mutation.payload), sort_keys=True, separators=(",", ":"),
                ensure_ascii=True,
            ),
            mutation.recorded_at, mutation.digest,
        ),
    )
    return cursor.rowcount


def append_memory_mutations_in_transaction(
    connection,
    records: tuple[MemoryMutation, ...],
    *,
    source_id: str,
    project_scope: str | None,
) -> int:
    """Append a bounded source page without opening or committing a transaction.

    This is intentionally not a general post-hoc journal helper: callers must
    already own a transaction containing the materialized source mutation.  A
    failure leaves rollback to that caller, preserving one atomic durable
    boundary.
    """
    if not connection.in_transaction:
        raise RuntimeError("memory replication append requires an active transaction")
    if type(records) is not tuple or not 1 <= len(records) <= _MAX_EXPORT_ROWS:
        raise ValueError("records must be a bounded non-empty tuple")
    for record in records:
        if not isinstance(record, MemoryMutation) or record.source_id != source_id:
            raise MemoryReplicationError("journal records must belong to this source")
        if project_scope is not None and record.project != project_scope:
            raise MemoryReplicationError("project scope cannot be widened")

    epoch, next_sequence, _scope = ensure_memory_replication_source(
        connection,
        source_id=source_id,
        project_scope=project_scope,
    )
    inserted = 0
    expected = next_sequence
    for record in records:
        if record.source_epoch != epoch:
            raise MemoryReplicationError("record source epoch is stale or not admitted")
        if record.sequence != expected:
            existing = connection.execute(
                "SELECT digest FROM memory_replication_log "
                "WHERE source_id=? AND sequence=?",
                (source_id, record.sequence),
            ).fetchone()
            if existing is None or existing[0] != record.digest:
                raise MemoryReplicationError("journal sequence is not contiguous")
            # Replaying an already committed prefix is idempotent; it must not
            # advance the append cursor a second time.
            continue
        latest = connection.execute(
            "SELECT MAX(version) FROM memory_replication_log "
            "WHERE source_id=? AND project=? AND entity_kind=? AND entity_id=?",
            (
                record.source_id,
                record.project,
                record.entity_kind,
                record.entity_id,
            ),
        ).fetchone()[0]
        if latest is not None and record.version <= latest:
            raise MemoryReplicationError("entity version must advance")
        try:
            inserted += _insert_mutation(connection, record)
        except sqlite3.IntegrityError:
            existing = connection.execute(
                "SELECT digest FROM memory_replication_log "
                "WHERE source_id=? AND sequence=?",
                (source_id, record.sequence),
            ).fetchone()
            if existing is not None:
                if existing[0] != record.digest:
                    raise MemoryReplicationError(
                        "journal sequence conflicts with existing evidence"
                    )
            else:
                same_version = connection.execute(
                    "SELECT digest FROM memory_replication_log "
                    "WHERE source_id=? AND project=? AND entity_kind=? "
                    "AND entity_id=? AND version=?",
                    (
                        record.source_id,
                        record.project,
                        record.entity_kind,
                        record.entity_id,
                        record.version,
                    ),
                ).fetchone()
                if same_version is None or same_version[0] != record.digest:
                    raise MemoryReplicationError("journal entity version conflicts")
            if existing is not None and existing[0] == record.digest:
                # A duplicate at the append cursor is safe to replay without
                # changing the next sequence.
                pass
            elif existing is None:
                raise MemoryReplicationError("journal entity version conflicts")
            else:
                raise MemoryReplicationError(
                    "journal sequence conflicts with existing evidence"
                )
        expected += 1
    connection.execute(
        "UPDATE memory_replication_meta SET next_sequence=? WHERE source_id=?",
        (expected, source_id),
    )
    return inserted


def apply_memory_replication_batch_in_transaction(
    connection,
    batch: MemoryReplicationBatch,
    *,
    project_scope: str | None,
) -> int:
    """Persist one received page without opening or committing a transaction.

    A caller that also materializes the page into the normal memory tables must
    use this helper and keep both durable steps inside its own transaction.
    The standalone journal below wraps the same helper in its legacy
    connection-owned transaction.
    """
    if not getattr(connection, "in_transaction", False):
        raise RuntimeError("memory replication apply requires an active transaction")
    if not isinstance(batch, MemoryReplicationBatch):
        raise TypeError("memory replication batch is required")
    if len(batch.records) > _MAX_EXPORT_ROWS:
        raise MemoryReplicationError("replication batch exceeds the journal bound")
    if project_scope is not None:
        _validate_source(batch.source_id, project_scope)
        for record in batch.records:
            if record.project != project_scope:
                raise MemoryReplicationError("project scope cannot be widened")

    row = connection.execute(
        "SELECT source_epoch,next_sequence,project_scope "
        "FROM memory_replication_meta WHERE source_id=?",
        (batch.source_id,),
    ).fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO memory_replication_meta(source_id,source_epoch,next_sequence,project_scope) "
            "VALUES(?,?,?,?)",
            (batch.source_id, batch.source_epoch, 1, project_scope),
        )
        current_epoch, expected = batch.source_epoch, 1
    else:
        current_epoch, expected, persisted_scope = int(row[0]), int(row[1]), row[2]
        if persisted_scope != project_scope:
            raise MemoryReplicationError(
                "journal project scope conflicts with persisted scope"
            )
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
                raise MemoryReplicationError(
                    "replication sequence conflicts with existing evidence"
                )
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
            inserted += _insert_mutation(connection, record)
        except sqlite3.IntegrityError as exc:
            raise MemoryReplicationError("replication entity version conflicts") from exc
        expected += 1
    connection.execute(
        "UPDATE memory_replication_meta SET next_sequence=? WHERE source_id=?",
        (expected, batch.source_id),
    )
    return inserted


class SQLiteFactReplicationSink:
    """Atomically receive fact-only evidence and materialize normal facts.

    The caller owns the normal ``memory.db`` connection and explicitly creates
    this internal sink.  It has no configuration, listener, peer discovery, or
    retry loop.  A durable receipt is emitted only after the local journal and
    the normal fact projection commit in one SQLite transaction.
    """

    def __init__(
        self,
        identity: str,
        connection,
        *,
        project_scope: str,
        max_records: int = 256,
    ) -> None:
        if not isinstance(project_scope, str) or not project_scope:
            raise ValueError("fact replication requires a non-empty project scope")
        _validate_source(identity, project_scope)
        if not hasattr(connection, "execute") or not hasattr(connection, "in_transaction"):
            raise TypeError("a SQLite connection is required")
        # Python 3.12's ``autocommit=True`` leaves SQLite in autocommit mode:
        # ``Connection.commit()`` is a no-op even after this sink issues an
        # explicit BEGIN.  ``autocommit=False`` immediately opens another
        # transaction after commit, which violates this sink's owned
        # transaction boundary.  A receipt must follow a commit that another
        # connection can observe, so support only legacy transaction control.
        autocommit = getattr(connection, "autocommit", None)
        if autocommit is True:
            raise ValueError(
                "fact replication sink does not support sqlite autocommit=True"
            )
        if autocommit is False:
            raise ValueError(
                "fact replication sink does not support sqlite autocommit=False"
            )
        if connection.in_transaction:
            raise RuntimeError(
                "fact replication sink requires a connection outside a transaction"
            )
        if type(max_records) is not int or not 1 <= max_records <= _MAX_EXPORT_ROWS:
            raise ValueError(
                f"fact replication record bound must be within 1..{_MAX_EXPORT_ROWS}"
            )
        facts_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='facts'"
        ).fetchone()
        if facts_table is None:
            raise ValueError("fact replication sink requires the normal facts table")
        self.identity = identity
        self._connection = connection
        self.project_scope = project_scope
        self.max_records = max_records
        self._projection = SQLiteMemoryReplicationProjection(
            connection, project_scope=project_scope,
        )

    @contextmanager
    def _transaction(self):
        """Commit journal and projection together before a receipt exists."""
        if self._connection.in_transaction:
            # A savepoint would permit a receipt to escape before an outer
            # caller commits.  Reject that shape rather than claiming durable
            # receipt evidence for a transaction another owner can roll back.
            raise RuntimeError(
                "fact replication receipt requires a connection outside a transaction"
            )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.commit()
        except BaseException:
            # A failed COMMIT can leave the transaction open with its pending
            # journal and projection rows.  Make one bounded best-effort
            # rollback before returning any failure so an exact retry is not
            # blocked by this connection's stranded transaction.
            try:
                self._connection.rollback()
            except sqlite3.Error:
                pass
            raise

    def apply(self, batch: MemoryReplicationBatch) -> MemoryReplicaReceipt:
        if not isinstance(batch, MemoryReplicationBatch):
            raise TypeError("memory replication batch is required")
        if not batch.records:
            raise MemoryReplicationError("fact replication requires a non-empty batch")
        if len(batch.records) > self.max_records:
            raise MemoryReplicationError("fact replication batch exceeds the record bound")
        for record in batch.records:
            if record.entity_kind != "fact":
                raise MemoryReplicationError(
                    "fact replication accepts only fact mutations"
                )
            if record.project != self.project_scope:
                raise MemoryReplicationError("fact replication project scope cannot be widened")

        with self._transaction():
            inserted = apply_memory_replication_batch_in_transaction(
                self._connection,
                batch,
                project_scope=self.project_scope,
            )
            self._projection.apply(batch)
        return MemoryReplicaReceipt(
            replica_id=self.identity,
            source_id=batch.source_id,
            source_epoch=batch.source_epoch,
            next_sequence=batch.next_sequence,
            batch_digest=batch.digest,
            durable=True,
            inserted_records=inserted,
        )


class SQLiteMemoryReplicationJournal:
    """Bounded SQLite journal for one source identity and optional project."""

    def __init__(self, path: str | Path = ":memory:", *, source_id: str = "cluster-a", project_scope: str | None = None) -> None:
        self.path = str(path)
        self.source_id = source_id
        self.project_scope = project_scope
        self._memory_connection = None
        # Validate identities and scope before creating any state.
        _validate_source(source_id, project_scope)
        with self._session() as connection:
            connection.executescript(MEMORY_REPLICATION_DDL)
            ensure_memory_replication_source(
                connection,
                source_id=self.source_id,
                project_scope=self.project_scope,
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
        return _insert_mutation(connection, mutation)

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
            current, next_sequence, _persisted_scope = self._meta(connection)
            if source_epoch <= current:
                raise MemoryReplicationError("source epoch must advance")
            has_records = connection.execute(
                "SELECT 1 FROM memory_replication_log WHERE source_id=? LIMIT 1",
                (self.source_id,),
            ).fetchone() is not None
            if has_records or next_sequence != 1:
                # The bounded batch schema carries one source epoch for every
                # exported record.  With sequence keyed only by source, a
                # rollover after an allocated sequence would make a fresh
                # projection reject the first record of the new epoch.  Keep
                # the cursor truthful until an explicit bootstrap/archive
                # rollover protocol exists.
                raise MemoryReplicationError(
                    "source epoch can advance only from an empty bootstrap cursor"
                )
            connection.execute(
                "UPDATE memory_replication_meta SET source_epoch=? WHERE source_id=?",
                (source_epoch, self.source_id),
            )
            connection.commit()

    def append(self, records: tuple[MemoryMutation, ...]) -> int:
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = append_memory_mutations_in_transaction(
                connection,
                records,
                source_id=self.source_id,
                project_scope=self.project_scope,
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
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = apply_memory_replication_batch_in_transaction(
                connection,
                batch,
                project_scope=self.project_scope,
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


__all__ = [
    "MEMORY_REPLICATION_DDL",
    "SQLiteFactReplicationSink",
    "SQLiteMemoryReplicationJournal",
    "apply_memory_replication_batch_in_transaction",
    "append_memory_mutations_in_transaction",
    "ensure_memory_replication_source",
]
