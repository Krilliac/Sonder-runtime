"""Transactional outbox for per-domain SQLite databases (SPEC-5 §9).

Every state-owning database includes an outbox_events table.  State
mutations and their events are committed atomically.  The dispatcher
polls unpublished events and projects them into operations.db.

Usage inside an adapter:

    outbox = OutboxWriter(conn)
    with conn:                          # BEGIN IMMEDIATE
        conn.execute("UPDATE ...")      # domain mutation
        outbox.append(event)            # in the same transaction
                                        # COMMIT

Delivery:

    dispatcher = OutboxDispatcher(source_conn, ops_conn, domain="memory")
    dispatched = dispatcher.dispatch_batch(limit=100)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Sequence

from ....domain.common.events import DomainEvent

logger = logging.getLogger(__name__)


OUTBOX_DDL = """\
CREATE TABLE IF NOT EXISTS outbox_events (
    id              TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL,
    aggregate_type  TEXT NOT NULL,
    aggregate_id    TEXT NOT NULL,
    sequence        INTEGER NOT NULL,
    payload_json    TEXT NOT NULL,
    correlation_id  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    published_at    TEXT,
    UNIQUE(aggregate_type, aggregate_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
ON outbox_events(published_at, created_at);
"""

OPERATIONS_EVENT_DDL = """\
CREATE TABLE IF NOT EXISTS operation_events (
    id                TEXT PRIMARY KEY,
    source_event_id   TEXT UNIQUE,
    source_domain     TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    aggregate_type    TEXT NOT NULL,
    aggregate_id      TEXT NOT NULL,
    correlation_id    TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    occurred_at       TEXT NOT NULL,
    imported_at       TEXT NOT NULL
);
"""


class OutboxWriter:
    """Append domain events to the outbox within the caller's transaction."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append(self, event: DomainEvent) -> None:
        logger.debug(
            f"outbox append event_type={event.event_type!r} "
            f"aggregate={event.aggregate_type!r}/{event.aggregate_id!r} "
            f"seq={event.sequence}"
        )
        self._conn.execute(
            "INSERT INTO outbox_events "
            "(id, event_type, aggregate_type, aggregate_id, sequence, "
            " payload_json, correlation_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.id,
                event.event_type,
                event.aggregate_type,
                event.aggregate_id,
                event.sequence,
                json.dumps(event.payload, default=str),
                event.correlation_id,
                event.created_at,
            ),
        )

    def append_many(self, events: Sequence[DomainEvent]) -> None:
        logger.debug(f"outbox append_many count={len(events)}")
        for event in events:
            self.append(event)


class OutboxDispatcher:
    """Poll unpublished events from a source domain and project to operations.db.

    Delivery is at-least-once with idempotent consumers: the operations
    table uses UNIQUE(source_event_id) so a duplicate dispatch after
    crash is harmless.
    """

    def __init__(
        self,
        source_conn: sqlite3.Connection,
        ops_conn: sqlite3.Connection,
        domain: str,
    ) -> None:
        self._source = source_conn
        self._ops = ops_conn
        self._domain = domain

    def dispatch_batch(self, limit: int = 100) -> int:
        logger.debug(f"dispatch_batch domain={self._domain!r} limit={limit}")
        rows = self._source.execute(
            "SELECT id, event_type, aggregate_type, aggregate_id, "
            "       correlation_id, payload_json, created_at "
            "FROM outbox_events "
            "WHERE published_at IS NULL "
            "ORDER BY created_at "
            "LIMIT ?",
            (limit,),
        ).fetchall()

        if not rows:
            logger.debug(f"dispatch_batch domain={self._domain!r} found no unpublished events")
            return 0

        logger.debug(f"dispatch_batch domain={self._domain!r} found {len(rows)} unpublished events")
        if len(rows) >= limit:
            logger.warning(
                f"outbox dispatch batch is full, domain={self._domain!r}: "
                f"fetched {len(rows)}/{limit} events — backlog may be growing"
            )
        now = datetime.now(timezone.utc).isoformat()
        dispatched = 0

        for row in rows:
            event_id, etype, atype, aid, cid, payload, occurred = row
            try:
                self._ops.execute(
                    "INSERT INTO operation_events "
                    "(id, source_event_id, source_domain, event_type, "
                    " aggregate_type, aggregate_id, correlation_id, "
                    " payload_json, occurred_at, imported_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(__import__("uuid").uuid4()),
                        event_id,
                        self._domain,
                        etype,
                        atype,
                        aid,
                        cid,
                        payload,
                        occurred,
                        now,
                    ),
                )
                self._ops.commit()
            except sqlite3.IntegrityError:
                logger.warning(
                    f"outbox duplicate dispatch detected, domain={self._domain!r} "
                    f"event_id={event_id!r} — likely crash-recovery replay"
                )

            self._source.execute(
                "UPDATE outbox_events SET published_at = ? WHERE id = ?",
                (now, event_id),
            )
            self._source.commit()
            dispatched += 1

        logger.debug(f"dispatch_batch domain={self._domain!r} dispatched={dispatched}")
        if dispatched > 0:
            logger.info(f"outbox dispatched {dispatched} events from domain={self._domain!r}")
        return dispatched
