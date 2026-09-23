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
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect
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
_MAX_MIGRATION_ROWS = 1024
_MAX_MIGRATION_BYTES = 32 * 1024 * 1024


def _insert_fact_row(connection, fact_id: str, project: str, text: str, embedding) -> None:
    """Materialize the source-owned fact row at a patchable transaction stage."""
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        (fact_id, project, text, embedding),
    )


def _upsert_fact_row(connection, fact_id: str, project: str, text: str, embedding) -> None:
    """Replace a source-owned fact row at a patchable transaction stage."""
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "project=excluded.project,text=excluded.text,"
        "embedding=excluded.embedding",
        (fact_id, project, text, embedding),
    )


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


@dataclass(frozen=True)
class LegacyFactMigrationPlan:
    """A content-addressed, operator-approved legacy fact adoption plan."""

    source_id: str
    project_scope: str
    rows: tuple[tuple[str, str, str, bytes | None], ...]
    digest: str


def _migration_digest(
    source_id: str,
    project_scope: str,
    rows: tuple[tuple[str, str, str, bytes | None], ...],
) -> str:
    """Bind the operator approval to the complete migration scope."""
    return hashlib.sha256(json.dumps(
        {
            "source_id": source_id,
            "project_scope": project_scope,
            "rows": [
                [fact_id, project, text, embedding.hex() if embedding is not None else None]
                for fact_id, project, text, embedding in rows
            ],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def plan_legacy_fact_migration(connection, *, source_id: str, project_scope: str) -> LegacyFactMigrationPlan:
    """Capture unjournaled facts for one scope without changing the database."""
    if type(project_scope) is not str or not project_scope:
        raise MemoryReplicationError("authoritative fact scope is required")
    if type(source_id) is not str or not source_id:
        raise MemoryReplicationError("authoritative fact source is required")
    # Reject malformed identities before showing an operator an approvable
    # digest or creating any backup in the apply path.
    SQLiteAuthoritativeFactSource(source_id, project_scope=project_scope)
    total_bytes = connection.execute(
        "SELECT COALESCE(SUM(COALESCE(length(CAST(fact.id AS BLOB)), 0) + "
        "COALESCE(length(CAST(fact.project AS BLOB)), 0) + "
        "COALESCE(length(CAST(fact.text AS BLOB)), 0) + "
        "COALESCE(length(CAST(fact.embedding AS BLOB)), 0)), 0) "
        "FROM facts AS fact LEFT JOIN memory_authoritative_fact_state AS state "
        "ON state.project=fact.project AND state.fact_id=fact.id "
        "WHERE fact.project=? AND state.fact_id IS NULL",
        (project_scope,),
    ).fetchone()[0]
    if total_bytes > _MAX_MIGRATION_BYTES:
        raise MemoryReplicationError("legacy fact migration exceeds the byte limit")
    rows = connection.execute(
        "SELECT fact.id,fact.project,fact.text,fact.embedding "
        "FROM facts AS fact LEFT JOIN memory_authoritative_fact_state AS state "
        "ON state.project=fact.project AND state.fact_id=fact.id "
        "WHERE fact.project=? AND state.fact_id IS NULL ORDER BY fact.id LIMIT ?",
        (project_scope, _MAX_MIGRATION_ROWS + 1),
    ).fetchall()
    if len(rows) > _MAX_MIGRATION_ROWS:
        raise MemoryReplicationError("legacy fact migration exceeds the bounded plan size")
    normalized_rows = []
    for row in rows:
        fact_id, project, text, embedding = row
        if type(fact_id) is not str or not fact_id:
            raise MemoryReplicationError("legacy fact ID must be stored as text")
        if type(project) is not str or project != project_scope:
            raise MemoryReplicationError("legacy fact project must match the approved scope")
        if type(text) is not str:
            raise MemoryReplicationError("legacy fact text must be stored as text")
        if embedding is not None and type(embedding) is not bytes:
            raise MemoryReplicationError("legacy fact embedding must be stored as a blob")
        normalized_rows.append((fact_id, project, text, embedding))
    normalized = tuple(normalized_rows)
    if sum(
        len(fact_id.encode("utf-8")) + len(project.encode("utf-8"))
        + len(text.encode("utf-8")) + len(embedding or b"")
        for fact_id, project, text, embedding in normalized
    ) > _MAX_MIGRATION_BYTES:
        raise MemoryReplicationError("legacy fact migration exceeds the byte limit")
    for fact_id, project, text, embedding in normalized:
        MemoryMutation(
            source_id=source_id, source_epoch=1, sequence=1,
            entity_kind="fact", entity_id=fact_id, version=1,
            operation="upsert", project=project,
            payload=_fact_payload(text, embedding, None),
            recorded_at=_recorded_at(),
        )
    digest = _migration_digest(source_id, project_scope, normalized)
    return LegacyFactMigrationPlan(source_id, project_scope, normalized, digest)


def migrate_legacy_facts(
    connection,
    plan: LegacyFactMigrationPlan,
    *,
    backup_path: str | Path | None = None,
) -> int:
    """Adopt exactly the planned rows, with an optional SQLite backup first.

    This is an operator-offline protocol.  The caller must not have an open
    transaction.  A backup is created and integrity-checked before acquiring
    the write lock; the exact plan is then re-read under ``BEGIN IMMEDIATE``.
    Any writer that raced the backup invalidates the plan before mutation.
    The fact rows, source state, journal records, and derived indexes then
    share one commit.
    """
    if not isinstance(plan, LegacyFactMigrationPlan):
        raise TypeError("a LegacyFactMigrationPlan is required")
    if connection.in_transaction:
        raise MemoryReplicationError("legacy fact migration requires an idle connection")
    if len(plan.rows) > _MAX_MIGRATION_ROWS or plan.digest != _migration_digest(
        plan.source_id, plan.project_scope, plan.rows,
    ):
        raise MemoryReplicationError("legacy fact migration plan is stale")
    SQLiteAuthoritativeFactSource(plan.source_id, project_scope=plan.project_scope)
    if backup_path is None:
        raise MemoryReplicationError("legacy fact migration requires a new backup path")
    backup = Path(backup_path).expanduser()
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists() or backup.is_symlink():
        raise MemoryReplicationError("migration backup already exists")
    # Write the SQLite snapshot under an unpredictable same-directory name,
    # then publish it with a no-clobber hard link. No caller can swap the
    # requested path while SQLite is writing the backup bytes.
    fd, temporary_name = tempfile.mkstemp(
        prefix=".sonder-fact-backup-", suffix=".sqlite", dir=backup.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        target = owned_sqlite_connect(str(temporary))
        try:
            connection.backup(target)
            target.commit()
            integrity = target.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise MemoryReplicationError("migration backup failed integrity verification")
        finally:
            target.close()
        try:
            os.link(temporary, backup)
        except FileExistsError as exc:
            raise MemoryReplicationError("migration backup already exists") from exc
        except OSError as exc:
            raise MemoryReplicationError("atomic migration backup publication unavailable") from exc
    finally:
        temporary.unlink(missing_ok=True)
    connection.execute("BEGIN IMMEDIATE")
    try:
        # This check is deliberately inside the write transaction.  It is the
        # final guard against a writer changing the source after the backup.
        current = plan_legacy_fact_migration(
            connection, source_id=plan.source_id, project_scope=plan.project_scope,
        )
        if current.digest != plan.digest or current.rows != plan.rows:
            raise MemoryReplicationError("legacy fact migration plan is stale")
        source = SQLiteAuthoritativeFactSource(plan.source_id, project_scope=plan.project_scope)
        source._activate_in_transaction(connection)
        epoch, sequence = source._source_cursor(connection)
        records = []
        for fact_id, project, text, embedding in plan.rows:
            state = source._existing_state(connection, fact_id)
            if state is not None:
                raise MemoryReplicationError("legacy fact migration encountered an owned fact")
            record = MemoryMutation(
                source_id=plan.source_id, source_epoch=epoch, sequence=sequence,
                entity_kind="fact", entity_id=fact_id, version=1,
                operation="upsert", project=project,
                payload=_fact_payload(text, embedding, None), recorded_at=_recorded_at(),
            )
            records.append(record)
            sequence += 1
        if records:
            for record in records:
                source._store_state(connection, record)
                materialize_authoritative_fact_index(connection, record)
            append_memory_mutations_in_transaction(
                connection, tuple(records), source_id=plan.source_id,
                project_scope=plan.project_scope,
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return len(plan.rows)


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

    def activate(self, connection) -> None:
        """Persist this source/scope as the active fact-write authority.

        The marker is deliberately created by the real composition root before
        exposing its repository.  The legacy memory-store helpers use the same
        marker to refuse a journal-bypassing write for this exact project.
        """
        with self._transaction(connection):
            # Do not publish the fence over legacy rows.  An operator must run
            # the explicit bounded migration first; leaving the marker absent
            # keeps a failed activation restartable and avoids claiming that
            # unjournaled facts are authoritative.
            self._require_scoped_facts_authoritative(
                connection, verify_journal_evidence=True,
            )
            self._activate_in_transaction(connection)

    def _activate_in_transaction(self, connection) -> None:
        existing = connection.execute(
            "SELECT source_id FROM memory_authoritative_fact_activation "
            "WHERE project_scope=?",
            (self.project_scope,),
        ).fetchone()
        if existing is not None and existing[0] != self.source_id:
            raise MemoryReplicationError(
                "authoritative fact scope is already owned by another source"
            )
        connection.execute(
            "INSERT INTO memory_authoritative_fact_activation"
            "(project_scope,source_id) VALUES(?,?) ON CONFLICT(project_scope) "
            "DO UPDATE SET source_id=excluded.source_id",
            (self.project_scope, self.source_id),
        )
        self._source_cursor(connection)

    def _existing_state(self, connection, fact_id: str):
        state = connection.execute(
            "SELECT source_id,version,tombstoned "
            "FROM memory_authoritative_fact_state WHERE project=? AND fact_id=?",
            (self.project_scope, fact_id),
        ).fetchone()
        if state is not None and state[0] != self.source_id:
            raise MemoryReplicationError("fact is owned by another source")
        return state

    def _assert_active_source_owner(self, connection) -> None:
        """Reject direct mutations from a source that lost the project fence."""
        active = connection.execute(
            "SELECT source_id FROM memory_authoritative_fact_activation "
            "WHERE project_scope=?",
            (self.project_scope,),
        ).fetchone()
        if active is not None and active[0] != self.source_id:
            raise MemoryReplicationError(
                "authoritative fact scope is already owned by another source"
            )

    def _require_scoped_facts_authoritative(
        self, connection, *, verify_journal_evidence: bool = False,
    ) -> None:
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
        if not verify_journal_evidence:
            return
        missing_evidence = connection.execute(
            "SELECT 1 FROM facts AS fact "
            "JOIN memory_authoritative_fact_state AS state "
            "ON state.project=fact.project AND state.fact_id=fact.id "
            "WHERE fact.project=? AND state.source_id=? AND state.tombstoned=0 "
            "AND NOT EXISTS ("
            "SELECT 1 FROM memory_replication_log AS journal "
            "WHERE journal.source_id=state.source_id "
            "AND journal.project=fact.project AND journal.entity_kind='fact' "
            "AND journal.entity_id=fact.id AND journal.version=state.version "
            "AND journal.operation='upsert'"
            ") LIMIT 1",
            (self.project_scope, self.source_id),
        ).fetchone()
        if missing_evidence is not None:
            raise MemoryReplicationError(
                "existing project facts require authoritative journal evidence"
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
            self._assert_active_source_owner(connection)
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
                _upsert_fact_row(
                    connection, fact_id, self.project_scope, text, embedding,
                )
            else:
                # Preserve the legacy add operation's duplicate rejection when
                # this source is injected through MemoryRepositoryAdapter.
                _insert_fact_row(
                    connection, fact_id, self.project_scope, text, embedding,
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
            self._assert_active_source_owner(connection)
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


__all__ = [
    "AuthoritativeFactMetadata", "LegacyFactMigrationPlan",
    "SQLiteAuthoritativeFactSource", "migrate_legacy_facts",
    "plan_legacy_fact_migration",
]
