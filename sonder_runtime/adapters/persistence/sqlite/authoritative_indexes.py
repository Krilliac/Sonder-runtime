"""Rebuildable scoped indexes derived only from committed fact mutations."""
from __future__ import annotations

import json
from datetime import datetime, timezone


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
    entities = metadata.get("entities", ())
    if not isinstance(entities, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() or len(item) > 160
        for item in entities
    ) or len(set(entities)) != len(entities):
        raise ValueError("authoritative entity metadata is invalid")
    decision = metadata.get("decision")
    if decision is not None and (
        not isinstance(decision, dict)
        or set(decision) != {"id", "value"}
        or any(not isinstance(item, str) or not item.strip() or len(item) > 2048 for item in decision.values())
    ):
        raise ValueError("authoritative decision metadata is invalid")
    return metadata


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


def rebuild_authoritative_fact_indexes(connection, *, project: str | None = None) -> int:
    """Clear and replay only durable journal rows into both derived indexes."""
    connection.execute("DELETE FROM memory_authoritative_entity_index" + (" WHERE project=?" if project else ""), ((project,) if project else ()))
    connection.execute("DELETE FROM memory_authoritative_decision_index" + (" WHERE project=?" if project else ""), ((project,) if project else ()))
    rows = connection.execute(
        "SELECT source_id,source_epoch,sequence,entity_kind,entity_id,version,"
        "operation,project,payload_json,recorded_at FROM memory_replication_log "
        + ("WHERE project=? " if project else "")
        + "ORDER BY source_id,sequence",
        (project,) if project else (),
    ).fetchall()
    for row in rows:
        record = type("JournalRecord", (), {
            "source_id": row[0], "source_epoch": row[1], "sequence": row[2],
            "entity_kind": row[3], "entity_id": row[4], "version": row[5],
            "operation": row[6], "project": row[7],
            "payload": json.loads(row[8]), "recorded_at": row[9],
        })()
        if record.entity_kind == "fact":
            materialize_authoritative_fact_index(connection, record)
    return len(rows)


def _valid_clause(now: str):
    return (
        " AND (valid_from IS NULL OR valid_from<=?)"
        " AND (valid_until IS NULL OR valid_until>?)", (now, now)
    )


def entities_for_project(connection, project: str, *, entity_id: str | None = None, now: str | None = None):
    now = now or datetime.now(timezone.utc).isoformat()
    clause, args = _valid_clause(now)
    extra = " AND entity_id=?" if entity_id is not None else ""
    values = connection.execute(
        "SELECT project,entity_id,fact_id,source_id,source_epoch,sequence,version,"
        "valid_from,valid_until,supersedes,provenance_json FROM "
        "memory_authoritative_entity_index WHERE project=? AND tombstoned=0"
        + extra + clause + " ORDER BY entity_id,fact_id",
        (project, *((entity_id,) if entity_id is not None else ()), *args),
    ).fetchall()
    return [dict(row) for row in values]


def decisions_for_project(connection, project: str, *, decision_id: str | None = None, now: str | None = None):
    now = now or datetime.now(timezone.utc).isoformat()
    clause, args = _valid_clause(now)
    extra = " AND decision_id=?" if decision_id is not None else ""
    values = connection.execute(
        "SELECT project,decision_id,fact_id,source_id,source_epoch,sequence,version,"
        "decision_json,valid_from,valid_until,supersedes,provenance_json FROM "
        "memory_authoritative_decision_index WHERE project=? AND tombstoned=0"
        + extra + clause + " ORDER BY decision_id,fact_id",
        (project, *((decision_id,) if decision_id is not None else ()), *args),
    ).fetchall()
    return [dict(row) for row in values]


__all__ = [
    "AUTHORITATIVE_INDEX_DDL", "materialize_authoritative_fact_index",
    "rebuild_authoritative_fact_indexes", "entities_for_project", "decisions_for_project",
]
