"""Rebuildable scoped indexes derived only from committed fact mutations."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone

from sonder_runtime.domain.memory.replication import MemoryMutation, MemoryReplicationError
from sonder_runtime.domain.memory.authoritative_fact_metadata import AuthoritativeFactMetadata


_MAX_REBUILD_ROWS = 100_000
MAX_INDEX_RESULTS = 16
_MAX_INDEX_OFFSET = 100_000


AUTHORITATIVE_INDEX_DDL = """
CREATE TABLE IF NOT EXISTS memory_authoritative_entity_index (
    project TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_epoch INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    version INTEGER NOT NULL,
    valid_from TEXT,
    valid_until TEXT,
    supersedes TEXT,
    provenance_json TEXT NOT NULL,
    tombstoned INTEGER NOT NULL CHECK(tombstoned IN (0, 1)),
    PRIMARY KEY(project, entity_id, fact_id)
);
CREATE INDEX IF NOT EXISTS idx_authoritative_entity_scope
ON memory_authoritative_entity_index(project, entity_id, tombstoned);
CREATE TABLE IF NOT EXISTS memory_authoritative_decision_index (
    project TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_epoch INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    version INTEGER NOT NULL,
    decision_json TEXT NOT NULL,
    valid_from TEXT,
    valid_until TEXT,
    supersedes TEXT,
    provenance_json TEXT NOT NULL,
    tombstoned INTEGER NOT NULL CHECK(tombstoned IN (0, 1)),
    PRIMARY KEY(project, decision_id, fact_id)
);
CREATE INDEX IF NOT EXISTS idx_authoritative_decision_scope
ON memory_authoritative_decision_index(project, decision_id, tombstoned);
"""


def _metadata(record):
    payload = record.payload if hasattr(record, "payload") else record.get("payload", {})
    metadata = payload.get("metadata", {}) if hasattr(payload, "get") else {}
    if not isinstance(metadata, dict):
        raise ValueError("authoritative metadata must be an object")
    if not metadata:
        return {}
    return AuthoritativeFactMetadata.from_payload(metadata).as_payload()


def _provenance(value):
    return json.dumps(tuple(value or ()), ensure_ascii=True, separators=(",", ":"))


def materialize_authoritative_fact_index(connection, record) -> None:
    """Apply one committed-source mutation inside its caller-owned transaction."""
    project, fact_id = record.project, record.entity_id
    if record.operation == "delete":
        connection.execute(
            "UPDATE memory_authoritative_entity_index SET tombstoned=1,source_id=?,"
            "source_epoch=?,sequence=?,version=? "
            "WHERE project=? AND fact_id=?",
            (record.source_id, record.source_epoch, record.sequence, record.version, project, fact_id),
        )
        connection.execute(
            "UPDATE memory_authoritative_decision_index SET tombstoned=1,source_id=?,"
            "source_epoch=?,sequence=?,version=? "
            "WHERE project=? AND fact_id=?",
            (record.source_id, record.source_epoch, record.sequence, record.version, project, fact_id),
        )
        return
    metadata = _metadata(record)
    connection.execute(
        "DELETE FROM memory_authoritative_entity_index WHERE project=? AND fact_id=?",
        (project, fact_id),
    )
    connection.execute(
        "DELETE FROM memory_authoritative_decision_index WHERE project=? AND fact_id=?",
        (project, fact_id),
    )
    common = (
        project, fact_id, record.source_id, record.source_epoch,
        record.sequence, record.version, metadata.get("valid_from"),
        metadata.get("valid_until"), metadata.get("supersedes"),
        _provenance(metadata.get("provenance")), 0,
    )
    for entity_id in metadata.get("entities", ()):
        connection.execute(
            "INSERT INTO memory_authoritative_entity_index "
            "(project,entity_id,fact_id,source_id,source_epoch,sequence,version,"
            "valid_from,valid_until,supersedes,provenance_json,tombstoned) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (project, entity_id, *common[1:]),
        )
    decision = metadata.get("decision")
    if decision:
        connection.execute(
            "INSERT INTO memory_authoritative_decision_index "
            "(project,decision_id,fact_id,source_id,source_epoch,sequence,version,"
            "decision_json,valid_from,valid_until,supersedes,provenance_json,tombstoned) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project, decision["id"], fact_id, record.source_id, record.source_epoch,
             record.sequence, record.version,
             json.dumps(decision, sort_keys=True, separators=(",", ":")),
             metadata.get("valid_from"), metadata.get("valid_until"),
             metadata.get("supersedes"), _provenance(metadata.get("provenance")), 0),
        )


@contextmanager
def _rebuild_transaction(connection):
    nested = bool(connection.in_transaction)
    if nested:
        connection.execute("SAVEPOINT sonder_fact_index_rebuild")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        if nested:
            connection.execute("ROLLBACK TO SAVEPOINT sonder_fact_index_rebuild")
            connection.execute("RELEASE SAVEPOINT sonder_fact_index_rebuild")
        else:
            connection.rollback()
        raise
    else:
        if nested:
            connection.execute("RELEASE SAVEPOINT sonder_fact_index_rebuild")
        else:
            connection.commit()


def rebuild_authoritative_fact_indexes(connection, *, project: str | None = None) -> int:
    """Atomically replay a bounded, digest-validated source journal."""
    if project is not None and (type(project) is not str or not project):
        raise ValueError("project must be an exact non-empty scope")
    where = "WHERE project=? " if project else ""
    args = (project,) if project else ()
    with _rebuild_transaction(connection):
        count = connection.execute(
            "SELECT COUNT(*) FROM memory_replication_log " + where, args,
        ).fetchone()[0]
        if count > _MAX_REBUILD_ROWS:
            raise MemoryReplicationError("fact index rebuild exceeds journal row bound")
        conflict = connection.execute(
            "SELECT 1 FROM memory_replication_log WHERE entity_kind='fact' "
            + ("AND project=? " if project else "")
            + "GROUP BY project,entity_id HAVING COUNT(DISTINCT source_id)>1 LIMIT 1",
            args,
        ).fetchone()
        if conflict is not None:
            raise MemoryReplicationError("fact index rebuild found conflicting sources")
        connection.execute(
            "DELETE FROM memory_authoritative_entity_index" + (" WHERE project=?" if project else ""), args,
        )
        connection.execute(
            "DELETE FROM memory_authoritative_decision_index" + (" WHERE project=?" if project else ""), args,
        )
        cursor = connection.execute(
            "SELECT source_id,source_epoch,sequence,entity_kind,entity_id,version,"
            "operation,project,payload_json,recorded_at,digest "
            "FROM memory_replication_log " + where + "ORDER BY source_id,sequence", args,
        )
        for row in cursor:
            record = MemoryMutation(
                source_id=row[0], source_epoch=row[1], sequence=row[2],
                entity_kind=row[3], entity_id=row[4], version=row[5],
                operation=row[6], project=row[7], payload=json.loads(row[8]),
                recorded_at=row[9],
            )
            if record.digest != row[10]:
                raise MemoryReplicationError("fact index rebuild found a changed journal record")
            if record.entity_kind == "fact":
                materialize_authoritative_fact_index(connection, record)
    return count


def _valid_clause(now: str | None):
    if now is None:
        observed = datetime.now(timezone.utc)
    else:
        if not isinstance(now, str) or not now.strip() or len(now) > 64:
            raise ValueError("index time must be a bounded timestamp")
        try:
            observed = datetime.fromisoformat(now.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("index time must be an ISO timestamp") from exc
        if observed.tzinfo is None:
            raise ValueError("index time requires a timezone")
    now = observed.astimezone(timezone.utc).isoformat()
    return (
        " AND (valid_from IS NULL OR valid_from<=?)"
        " AND (valid_until IS NULL OR valid_until>?)", (now, now)
    )


def _index_offset(offset: int) -> int:
    if type(offset) is not int or not 0 <= offset <= _MAX_INDEX_OFFSET:
        raise ValueError("index offset must be within 0..100000")
    return offset


def entities_for_project(connection, project: str, *, entity_id: str | None = None, now: str | None = None, offset: int = 0):
    clause, args = _valid_clause(now)
    offset = _index_offset(offset)
    extra = " AND entity_id=?" if entity_id is not None else ""
    values = connection.execute(
        "SELECT project,entity_id,fact_id,source_id,source_epoch,sequence,version,"
        "valid_from,valid_until,supersedes,provenance_json FROM "
        "memory_authoritative_entity_index WHERE project=? AND tombstoned=0"
        + extra + clause + " ORDER BY entity_id,fact_id LIMIT ? OFFSET ?",
        (project, *((entity_id,) if entity_id is not None else ()), *args, MAX_INDEX_RESULTS, offset),
    ).fetchall()
    return [dict(row) for row in values]


def decisions_for_project(connection, project: str, *, decision_id: str | None = None, now: str | None = None, offset: int = 0):
    clause, args = _valid_clause(now)
    offset = _index_offset(offset)
    extra = " AND decision_id=?" if decision_id is not None else ""
    values = connection.execute(
        "SELECT project,decision_id,fact_id,source_id,source_epoch,sequence,version,"
        "decision_json,valid_from,valid_until,supersedes,provenance_json FROM "
        "memory_authoritative_decision_index WHERE project=? AND tombstoned=0"
        + extra + clause + " ORDER BY decision_id,fact_id LIMIT ? OFFSET ?",
        (project, *((decision_id,) if decision_id is not None else ()), *args, MAX_INDEX_RESULTS, offset),
    ).fetchall()
    return [dict(row) for row in values]


__all__ = [
    "AUTHORITATIVE_INDEX_DDL", "MAX_INDEX_RESULTS", "materialize_authoritative_fact_index",
    "rebuild_authoritative_fact_indexes", "entities_for_project", "decisions_for_project",
]
