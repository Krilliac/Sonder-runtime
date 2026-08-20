import pytest

from sonder_runtime.application.persistence.outbox_cas import (
    InMemoryOutboxCASRepository,
    OutboxEvent,
    PersistenceError,
    TransactionNeutralRecord,
    UnsupportedSchemaVersion,
)


def record(revision=0, payload=None):
    return TransactionNeutralRecord("session-1", revision, payload or {"status": "open"})


def event(revision=1, event_id="event-1"):
    return OutboxEvent(event_id, "session-1", "session.updated", revision, {"ok": True}, "2026-08-20T00:00:00Z")


def test_record_and_outbox_event_are_immutable_at_boundary():
    payload = {"nested": {"value": 1}}
    item = record(payload=payload)
    payload["nested"]["value"] = 9
    assert item.payload["nested"] == {"value": 1}
    with pytest.raises(TypeError):
        item.payload["nested"]["value"] = 2
    with pytest.raises(TypeError):
        item.payload["status"] = "closed"
    with pytest.raises(TypeError):
        event().payload["ok"] = False


def test_atomic_revision_cas_stages_matching_outbox_event():
    repository = InMemoryOutboxCASRepository()
    assert repository.append(record(0), event(0), expected_revision=-1).revision == 0
    assert repository.get("session-1").payload["status"] == "open"
    assert repository.outbox() == (event(0),)


def test_stale_revision_is_rejected_without_partial_record_or_event_write():
    repository = InMemoryOutboxCASRepository()
    assert repository.append(record(0), event(0), expected_revision=-1)
    assert repository.append(record(1, {"status": "closed"}), event(1, "event-2"), expected_revision=-1) is None
    assert repository.get("session-1").revision == 0
    assert len(repository.outbox()) == 1


def test_record_and_event_identity_revision_must_match():
    with pytest.raises(PersistenceError):
        InMemoryOutboxCASRepository().append(record(0), event(1), expected_revision=-1)


def test_future_schema_versions_are_rejected():
    with pytest.raises(UnsupportedSchemaVersion):
        TransactionNeutralRecord("session-1", 0, {}, schema_version=2)
    with pytest.raises(UnsupportedSchemaVersion):
        OutboxEvent("e", "session-1", "changed", 1, {}, "now", schema_version=2)
