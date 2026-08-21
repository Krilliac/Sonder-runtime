from pathlib import Path

import pytest

from sonder_runtime.adapters.persistence.sqlite.cross_domain import SQLiteCrossDomainCoordinator
from sonder_runtime.application.persistence.cross_domain import (
    CoordinationIdempotencyConflict,
    CoordinationRevisionConflict,
    CrossDomainWrite,
)
from sonder_runtime.application.persistence.outbox_cas import OutboxEvent, TransactionNeutralRecord


def write(domain, aggregate, revision, event_id):
    return CrossDomainWrite(
        domain,
        TransactionNeutralRecord(aggregate, revision, {"state": domain}),
        OutboxEvent(event_id, aggregate, "state.changed", revision, {"domain": domain}, "2026-08-21T00:00:00Z"),
        expected_revision=revision - 1,
    )


def test_data002_commits_record_and_outbox_for_all_domains_atomically(tmp_path: Path):
    coordinator = SQLiteCrossDomainCoordinator(tmp_path / "coord.db")
    result = coordinator.coordinate("op-1", (write("memory", "m-1", 0, "e-m"), write("jobs", "j-1", 0, "e-j")))
    assert result.committed and not result.replayed
    with __import__("sqlite3").connect(tmp_path / "coord.db") as db:
        assert db.execute("SELECT revision FROM coord_memory_records").fetchone() == (0,)
        assert db.execute("SELECT event_id FROM coord_jobs_outbox").fetchone() == ("e-j",)


def test_data002_replay_is_idempotent_and_conflicting_key_fails_closed(tmp_path):
    coordinator = SQLiteCrossDomainCoordinator(tmp_path / "coord.db")
    writes = (write("memory", "m-1", 0, "e-m"), write("jobs", "j-1", 0, "e-j"))
    first = coordinator.coordinate("op-1", writes)
    replay = coordinator.coordinate("op-1", writes)
    assert replay.replayed and replay.fingerprint == first.fingerprint
    with pytest.raises(CoordinationIdempotencyConflict):
        coordinator.coordinate("op-1", (write("memory", "m-2", 0, "e-2"), write("jobs", "j-1", 0, "e-j")))


def test_data002_revision_failure_rolls_back_every_domain(tmp_path):
    coordinator = SQLiteCrossDomainCoordinator(tmp_path / "coord.db")
    with pytest.raises(CoordinationRevisionConflict):
        coordinator.coordinate("op-1", (write("memory", "m-1", 0, "e-m"), write("jobs", "j-1", 2, "e-j")))
    with __import__("sqlite3").connect(tmp_path / "coord.db") as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='coord_memory_records'").fetchone() is None
        assert db.execute("SELECT name FROM sqlite_master WHERE name='cross_domain_operations'").fetchone() is not None


def test_data002_advances_existing_domain_revisions_with_a_matching_outbox(tmp_path):
    coordinator = SQLiteCrossDomainCoordinator(tmp_path / "coord.db")
    coordinator.coordinate("op-1", (write("memory", "m-1", 0, "e-1"),))
    coordinator.coordinate("op-2", (write("memory", "m-1", 1, "e-2"),))
    with __import__("sqlite3").connect(tmp_path / "coord.db") as db:
        assert db.execute("SELECT revision FROM coord_memory_records").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM coord_memory_outbox").fetchone() == (2,)
