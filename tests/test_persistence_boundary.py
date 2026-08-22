from __future__ import annotations

import sqlite3

import pytest

from sonder_runtime.adapters.persistence.sqlite.graph import build_sqlite_persistence_facade
from sonder_runtime.application.artifacts.immutable_manifest import ArtifactRecord
from sonder_runtime.application.persistence.cross_domain import CrossDomainWrite
from sonder_runtime.application.persistence.facade import (
    CrossDatabaseTransactionError,
    PersistenceBoundaryError,
)
from sonder_runtime.application.persistence.outbox_cas import OutboxEvent, TransactionNeutralRecord


def _write(domain: str, aggregate: str, revision: int, event_id: str) -> CrossDomainWrite:
    return CrossDomainWrite(
        domain,
        TransactionNeutralRecord(aggregate, revision, {"domain": domain}),
        OutboxEvent(event_id, aggregate, "changed", revision, {}, "now"),
        expected_revision=revision - 1,
    )


def test_sqlite_graph_binds_each_domain_to_its_own_atomic_cas_outbox(tmp_path):
    facade = build_sqlite_persistence_facade(tmp_path)
    record = TransactionNeutralRecord("a-1", 0, {"state": "open"})
    event = OutboxEvent("e-1", "a-1", "opened", 0, {}, "now")

    assert facade.append("memory", record, event, expected_revision=-1) == record
    assert facade.get("memory", "a-1") == record
    assert facade.domain("memory").repository.outbox() == (event,)
    assert facade.registry.owner_for(tmp_path / "memory.db") == "memory"
    with sqlite3.connect(tmp_path / "automation.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM persistence_records").fetchone() == (0,)


def test_stale_cas_is_fail_closed_and_does_not_append_event(tmp_path):
    facade = build_sqlite_persistence_facade(tmp_path)
    first = TransactionNeutralRecord("a-1", 0, {})
    first_event = OutboxEvent("e-1", "a-1", "opened", 0, {}, "now")
    facade.append("memory", first, first_event, expected_revision=-1)

    stale = TransactionNeutralRecord("a-1", 1, {"state": "stale"})
    stale_event = OutboxEvent("e-2", "a-1", "stale", 1, {}, "now")
    assert facade.append("memory", stale, stale_event, expected_revision=-1) is None
    assert facade.domain("memory").repository.outbox() == (first_event,)


def test_facade_rejects_cross_database_coordination_before_adapter_work(tmp_path):
    facade = build_sqlite_persistence_facade(tmp_path)
    with pytest.raises(PersistenceBoundaryError, match="coordinator is not composed"):
        facade.coordinate("op-1", (_write("memory", "m", 0, "e-m"),))

    # A coordinator is deliberately not composed for the per-domain graph:
    # callers cannot accidentally turn distinct stores into one transaction.
    assert facade.registry.owner_for(tmp_path / "operations.db") == "operations"


def test_artifact_manifest_is_deterministic_and_hash_bound(tmp_path):
    facade = build_sqlite_persistence_facade(tmp_path)
    first = ArtifactRecord.from_bytes("b", b"two")
    second = ArtifactRecord.from_bytes("a", b"one")
    manifest = facade.artifact_manifest((first, second))
    assert manifest.reference("a").manifest_digest == manifest.digest
    with pytest.raises((AttributeError, TypeError)):
        manifest.entries[0].digest = "0" * 64  # type: ignore[misc]


def test_facade_rejects_an_incomplete_repository_graph(tmp_path):
    from sonder_runtime.application.persistence.domain_ownership import default_domain_store_registry
    from sonder_runtime.application.persistence.facade import PersistenceFacade

    registry = default_domain_store_registry(tmp_path)
    with pytest.raises(PersistenceBoundaryError, match="does not match"):
        PersistenceFacade(registry, {})
