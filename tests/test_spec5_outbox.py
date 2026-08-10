"""SPEC-5 WP2: Transactional outbox contract tests."""
from __future__ import annotations

import json
import sqlite3

import pytest

from sonder_runtime.domain.common.events import DomainEvent
from sonder_runtime.adapters.persistence.sqlite.outbox import (
    OUTBOX_DDL,
    OPERATIONS_EVENT_DDL,
    OutboxWriter,
    OutboxDispatcher,
)


@pytest.fixture
def source_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(OUTBOX_DDL)
    yield conn
    conn.close()


@pytest.fixture
def ops_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(OPERATIONS_EVENT_DDL)
    yield conn
    conn.close()


def _make_event(seq: int = 1, **kwargs) -> DomainEvent:
    defaults = dict(
        event_type="interaction.created",
        aggregate_type="interaction",
        aggregate_id="agg-1",
        sequence=seq,
        payload={"key": "value"},
        correlation_id="corr-1",
    )
    defaults.update(kwargs)
    return DomainEvent(**defaults)


class TestOutboxWriter:
    def test_append_stores_event(self, source_db):
        writer = OutboxWriter(source_db)
        event = _make_event()
        with source_db:
            writer.append(event)
        row = source_db.execute("SELECT * FROM outbox_events").fetchone()
        assert row is not None
        assert row[0] == event.id
        assert row[1] == "interaction.created"
        assert row[8] is None  # published_at

    def test_append_many(self, source_db):
        writer = OutboxWriter(source_db)
        events = [_make_event(seq=i) for i in range(1, 4)]
        with source_db:
            writer.append_many(events)
        count = source_db.execute("SELECT count(*) FROM outbox_events").fetchone()[0]
        assert count == 3

    def test_unique_aggregate_sequence(self, source_db):
        writer = OutboxWriter(source_db)
        event1 = _make_event(seq=1)
        with source_db:
            writer.append(event1)
        event2 = _make_event(seq=1)
        with pytest.raises(sqlite3.IntegrityError):
            with source_db:
                writer.append(event2)

    def test_payload_serialized_as_json(self, source_db):
        writer = OutboxWriter(source_db)
        event = _make_event(payload={"nested": {"a": 1}})
        with source_db:
            writer.append(event)
        raw = source_db.execute(
            "SELECT payload_json FROM outbox_events"
        ).fetchone()[0]
        assert json.loads(raw) == {"nested": {"a": 1}}


class TestOutboxDispatcher:
    def test_dispatch_projects_to_operations(self, source_db, ops_db):
        writer = OutboxWriter(source_db)
        event = _make_event()
        with source_db:
            writer.append(event)

        dispatcher = OutboxDispatcher(source_db, ops_db, domain="memory")
        dispatched = dispatcher.dispatch_batch()
        assert dispatched == 1

        op_row = ops_db.execute("SELECT * FROM operation_events").fetchone()
        assert op_row is not None
        assert op_row[1] == event.id  # source_event_id
        assert op_row[2] == "memory"  # source_domain

    def test_published_at_marked(self, source_db, ops_db):
        writer = OutboxWriter(source_db)
        with source_db:
            writer.append(_make_event())

        dispatcher = OutboxDispatcher(source_db, ops_db, domain="memory")
        dispatcher.dispatch_batch()

        published = source_db.execute(
            "SELECT published_at FROM outbox_events"
        ).fetchone()[0]
        assert published is not None

    def test_no_events_returns_zero(self, source_db, ops_db):
        dispatcher = OutboxDispatcher(source_db, ops_db, domain="memory")
        assert dispatcher.dispatch_batch() == 0

    def test_duplicate_dispatch_is_harmless(self, source_db, ops_db):
        writer = OutboxWriter(source_db)
        event = _make_event()
        with source_db:
            writer.append(event)

        dispatcher = OutboxDispatcher(source_db, ops_db, domain="memory")
        dispatcher.dispatch_batch()

        # Simulate crash: reset published_at
        source_db.execute("UPDATE outbox_events SET published_at = NULL")
        source_db.commit()

        # Re-dispatch — should not duplicate in operations
        dispatched = dispatcher.dispatch_batch()
        assert dispatched == 1

        count = ops_db.execute(
            "SELECT count(*) FROM operation_events"
        ).fetchone()[0]
        assert count == 1  # still just one

    def test_already_published_skipped(self, source_db, ops_db):
        writer = OutboxWriter(source_db)
        with source_db:
            writer.append(_make_event())

        dispatcher = OutboxDispatcher(source_db, ops_db, domain="memory")
        dispatcher.dispatch_batch()

        # Second dispatch should find nothing unpublished
        assert dispatcher.dispatch_batch() == 0

    def test_aggregate_ordering(self, source_db, ops_db):
        writer = OutboxWriter(source_db)
        with source_db:
            writer.append_many([
                _make_event(seq=1, aggregate_id="a"),
                _make_event(seq=2, aggregate_id="a"),
                _make_event(seq=1, aggregate_id="b"),
            ])

        dispatcher = OutboxDispatcher(source_db, ops_db, domain="memory")
        dispatched = dispatcher.dispatch_batch()
        assert dispatched == 3

        rows = ops_db.execute(
            "SELECT aggregate_id FROM operation_events ORDER BY occurred_at"
        ).fetchall()
        ids = [r[0] for r in rows]
        assert ids == ["a", "a", "b"]


class TestCrashSafety:
    def test_committed_event_survives_crash_simulation(self, source_db, ops_db):
        """After source COMMIT, event is durable even if dispatch hasn't run."""
        writer = OutboxWriter(source_db)
        with source_db:
            writer.append(_make_event())
        # Simulate process restart — new dispatcher picks up unpublished
        dispatcher = OutboxDispatcher(source_db, ops_db, domain="memory")
        assert dispatcher.dispatch_batch() == 1

    def test_uncommitted_mutation_absent(self, source_db):
        """If the transaction rolls back, no event exists."""
        writer = OutboxWriter(source_db)
        try:
            source_db.execute("BEGIN IMMEDIATE")
            writer.append(_make_event())
            raise RuntimeError("simulated crash")
        except RuntimeError:
            source_db.rollback()
        count = source_db.execute(
            "SELECT count(*) FROM outbox_events"
        ).fetchone()[0]
        assert count == 0
