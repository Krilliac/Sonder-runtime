"""Atomic source-side mutations for the deliberately narrow fact write set.

This adapter is intentionally not a replication service.  It has no receiver,
peer discovery, configuration, HTTP route, or retry loop.  A composition root
must explicitly inject one instance into a memory repository before the
supported ``fact`` writes use this source contract.
"""
from __future__ import annotations

from array import array
from contextlib import contextmanager
from datetime import datetime, timezone
import math

from sonder_runtime.adapters.persistence.sqlite.memory_replication import (
    append_memory_mutations_in_transaction,
    ensure_memory_replication_source,
)
from sonder_runtime.domain.memory.replication import (
    MemoryMutation,
    MemoryReplicationError,
)
from sonder_runtime.domain.memory.authoritative_fact_metadata import AuthoritativeFactMetadata
from .authoritative_indexes import materialize_authoritative_fact_index


_MAX_EMBEDDING = 16_384
_SAVEPOINT = "sonder_authoritative_fact_write"


def _recorded_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fact_payload(text: object, embedding: object, metadata: AuthoritativeFactMetadata | None) -> dict[str, object]:
    if not isinstance(text, str) or not text.strip():
        raise MemoryReplicationError("fact text must be a bounded non-empty string")
    if metadata is not None and not isinstance(metadata, AuthoritativeFactMetadata):
        raise MemoryReplicationError("fact metadata must use the typed authoritative contract")
    payload: dict[str, object] = {"text": text, "embedding": None}
    if metadata is not None:
        payload["metadata"] = metadata.as_payload()
    if embedding is None:
        return payload
    if isinstance(embedding, memoryview):
        embedding = embedding.tobytes()
    if not isinstance(embedding, (bytes, bytearray)):
        raise MemoryReplicationError("fact embedding must be a SQLite float blob")
    raw = bytes(embedding)
    if not raw or len(raw) % 4:
        raise MemoryReplicationError("fact embedding must be a non-empty float blob")
    values = array("f")
    try:
        values.frombytes(raw)
    except (EOFError, ValueError) as exc:
        raise MemoryReplicationError("fact embedding must be a float blob") from exc
    if (
        not 1 <= len(values) <= _MAX_EMBEDDING
        or not all(math.isfinite(value) for value in values)
        or not any(values)
    ):
        raise MemoryReplicationError("fact embedding is not projection-safe")
    payload["embedding"] = list(values)
    return payload


class SQLiteAuthoritativeFactSource:
    """Write project-scoped facts and their source evidence atomically.

    ``fact`` is the complete supported entity set for this first source-side
    slice.  Interactions, outcomes, preferences, and lessons intentionally
    retain their legacy paths until they receive equivalent atomic contracts.
    """

    supported_entity_kinds = ("fact",)

    def __init__(self, source_id: str, *, project_scope: str) -> None:
        if not isinstance(project_scope, str) or not project_scope:
            raise MemoryReplicationError("authoritative fact scope is required")
        # Let the existing immutable wire contract validate the source identity
        # and exact project grammar before a caller can mutate a database.
        MemoryMutation(
            source_id=source_id,
            source_epoch=1,
            sequence=1,
            entity_kind="fact",
            entity_id="validation",
            version=1,
            operation="delete",
            project=project_scope,
            payload={},
            recorded_at=_recorded_at(),
        )
        self.source_id = source_id
        self.project_scope = project_scope

    @contextmanager
    def _transaction(self, connection):
        if not hasattr(connection, "execute") or not hasattr(
            connection, "in_transaction"
        ):
            raise TypeError("a SQLite connection is required")
        nested = bool(connection.in_transaction)
        if nested:
            connection.execute("SAVEPOINT " + _SAVEPOINT)
        else:
            connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            if nested:
                connection.execute("ROLLBACK TO SAVEPOINT " + _SAVEPOINT)
                connection.execute("RELEASE SAVEPOINT " + _SAVEPOINT)
            else:
                connection.rollback()
            raise
        else:
            if nested:
                connection.execute("RELEASE SAVEPOINT " + _SAVEPOINT)
            else:
                connection.commit()

    def _source_cursor(self, connection) -> tuple[int, int]:
        epoch, sequence, persisted_scope = ensure_memory_replication_source(
            connection,
            source_id=self.source_id,
            project_scope=self.project_scope,
        )
        if persisted_scope != self.project_scope:
            raise MemoryReplicationError(
                "authoritative fact scope conflicts with persisted source scope"
            )
        return epoch, sequence

    def _existing_state(self, connection, fact_id: str):
        state = connection.execute(
            "SELECT source_id,version,tombstoned "
            "FROM memory_authoritative_fact_state WHERE project=? AND fact_id=?",
            (self.project_scope, fact_id),
        ).fetchone()
        if state is not None and state[0] != self.source_id:
            raise MemoryReplicationError("fact is owned by another source")
        return state

    def _require_scoped_facts_authoritative(self, connection) -> None:
        """Refuse activation over facts with no matching source evidence.

        Existing project facts need an explicit migration before a live writer
        can claim this project is an authoritative replication source.
        """
        legacy = connection.execute(
            "SELECT 1 FROM facts AS fact LEFT JOIN "
            "memory_authoritative_fact_state AS state "
            "ON state.project=fact.project AND state.fact_id=fact.id "
            "WHERE fact.project=? AND "
            "(state.fact_id IS NULL OR state.source_id<>? OR state.tombstoned<>0) "
            "LIMIT 1",
            (self.project_scope, self.source_id),
        ).fetchone()
        if legacy is not None:
            raise MemoryReplicationError(
                "existing project facts require authoritative migration"
            )

    def _record(
        self,
        connection,
        fact_id: str,
        *,
        operation: str,
        payload: dict[str, object],
    ) -> MemoryMutation:
        epoch, sequence = self._source_cursor(connection)
        state = self._existing_state(connection, fact_id)
        version = 1 if state is None else int(state[1]) + 1
        return MemoryMutation(
            source_id=self.source_id,
            source_epoch=epoch,
            sequence=sequence,
            entity_kind="fact",
            entity_id=fact_id,
            version=version,
            operation=operation,
            project=self.project_scope,
            payload=payload,
            recorded_at=_recorded_at(),
        )

    def _store_state(self, connection, record: MemoryMutation) -> None:
        connection.execute(
            "INSERT INTO memory_authoritative_fact_state"
            "(project,fact_id,source_id,version,tombstoned) VALUES(?,?,?,?,?) "
            "ON CONFLICT(project,fact_id) DO UPDATE SET "
            "source_id=excluded.source_id,version=excluded.version,"
            "tombstoned=excluded.tombstoned",
            (
                self.project_scope,
                record.entity_id,
                self.source_id,
                record.version,
                1 if record.is_tombstone else 0,
            ),
        )

    def _write_fact(
        self,
        connection,
        fact_id: str,
        project: str,
        text: str,
        embedding=None,
        *,
        metadata: AuthoritativeFactMetadata | None = None,
        replace: bool = False,
    ) -> MemoryMutation:
        if project != self.project_scope:
            raise MemoryReplicationError("authoritative fact scope cannot be widened")
        payload = _fact_payload(text, embedding, metadata)
        with self._transaction(connection):
            record = self._record(
                connection,
                fact_id,
                operation="upsert",
                payload=payload,
            )
            self._require_scoped_facts_authoritative(connection)
            existing = connection.execute(
                "SELECT project FROM facts WHERE id=?", (fact_id,)
            ).fetchone()
            if existing is not None and existing[0] != self.project_scope:
                raise MemoryReplicationError(
                    "fact identity is already bound to another project"
                )
            if replace:
                connection.execute(
                    "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "project=excluded.project,text=excluded.text,"
                    "embedding=excluded.embedding",
                    (fact_id, self.project_scope, text, embedding),
                )
            else:
                # Preserve the legacy add operation's duplicate rejection when
                # this source is injected through MemoryRepositoryAdapter.
                connection.execute(
                    "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
                    (fact_id, self.project_scope, text, embedding),
                )
            self._store_state(connection, record)
            append_memory_mutations_in_transaction(
                connection,
                (record,),
                source_id=self.source_id,
                project_scope=self.project_scope,
            )
            materialize_authoritative_fact_index(connection, record)
        return record

    def add_fact(
        self,
        connection,
        fact_id: str,
        project: str,
        text: str,
        embedding=None,
        metadata: AuthoritativeFactMetadata | None = None,
    ) -> MemoryMutation:
        """Insert a new supported fact and its journal mutation together."""
        return self._write_fact(connection, fact_id, project, text, embedding, metadata=metadata)

    def upsert_fact(
        self,
        connection,
        fact_id: str,
        project: str,
        text: str,
        embedding=None,
        metadata: AuthoritativeFactMetadata | None = None,
    ) -> MemoryMutation:
        """Advance one supported fact's version without changing its scope."""
        return self._write_fact(
            connection, fact_id, project, text, embedding, metadata=metadata, replace=True
        )

    def delete_fact(self, connection, fact_id: str, project: str) -> bool:
        """Delete one supported fact and append a durable tombstone together."""
        if project != self.project_scope:
            raise MemoryReplicationError("authoritative fact scope cannot be widened")
        with self._transaction(connection):
            existing = connection.execute(
                "SELECT project FROM facts WHERE id=?", (fact_id,)
            ).fetchone()
            if existing is None:
                return False
            if existing[0] != self.project_scope:
                raise MemoryReplicationError(
                    "fact identity is already bound to another project"
                )
            record = self._record(
                connection,
                fact_id,
                operation="delete",
                payload={},
            )
            self._require_scoped_facts_authoritative(connection)
            deleted = connection.execute(
                "DELETE FROM facts WHERE id=? AND project=?",
                (fact_id, self.project_scope),
            ).rowcount
            if deleted != 1:
                raise RuntimeError("authoritative fact deletion lost its target")
            self._store_state(connection, record)
            append_memory_mutations_in_transaction(
                connection,
                (record,),
                source_id=self.source_id,
                project_scope=self.project_scope,
            )
            materialize_authoritative_fact_index(connection, record)
        return True

    def advance_epoch(self, connection, source_epoch: int) -> None:
        """Durably advance this source's epoch without mutating a fact."""
        if (
            isinstance(source_epoch, bool)
            or not isinstance(source_epoch, int)
            or source_epoch < 1
        ):
            raise MemoryReplicationError("source epoch must be positive")
        with self._transaction(connection):
            current_epoch, next_sequence = self._source_cursor(connection)
            if source_epoch <= current_epoch:
                raise MemoryReplicationError("source epoch must advance")
            has_records = connection.execute(
                "SELECT 1 FROM memory_replication_log WHERE source_id=? LIMIT 1",
                (self.source_id,),
            ).fetchone() is not None
            if has_records or next_sequence != 1:
                # This journal schema keys sequence by source, not epoch.  A
                # rollover after any allocated sequence would make a fresh
                # projection reject the first record of the new epoch.  A
                # fully pruned history still has an advanced cursor, so a
                # future rollover needs an explicit bootstrap/archive protocol.
                raise MemoryReplicationError(
                    "source epoch can advance only from an empty bootstrap cursor"
                )
            connection.execute(
                "UPDATE memory_replication_meta SET source_epoch=? "
                "WHERE source_id=?",
                (source_epoch, self.source_id),
            )


__all__ = ["AuthoritativeFactMetadata", "SQLiteAuthoritativeFactSource"]
