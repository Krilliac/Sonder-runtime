from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from sonder_runtime.domain.cluster_ownership import (
    OwnershipConflict,
    OwnershipError,
    TakeoverDenied,
)


class Clock:
    def __init__(self, value: float = 100.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


def _journal(
    tmp_path,
    clock,
    *,
    create=True,
    cluster_id="cluster-a",
    max_resources=10_000,
):
    from sonder_runtime.adapters.persistence.sqlite.cluster_ownership import (
        SQLiteClusterOwnershipJournal,
    )

    return SQLiteClusterOwnershipJournal(
        tmp_path / "ownership.sqlite",
        cluster_id=cluster_id,
        create=create,
        clock=clock,
        max_resources=max_resources,
    )


def test_acquire_is_durable_across_restart_and_idempotent_for_same_owner(tmp_path):
    clock = Clock()
    journal = _journal(tmp_path, clock)
    lease = journal.acquire("session", "session-1", "node-1", lease_seconds=10)

    assert journal.validate(lease).allowed
    reopened = _journal(tmp_path, clock, create=False)
    assert reopened.validate(lease).allowed
    assert reopened.acquire("session", "session-1", "node-1", lease_seconds=1) == lease


def test_acquire_conflict_is_serialized_between_journal_instances(tmp_path):
    clock = Clock()
    first = _journal(tmp_path, clock)
    second = _journal(tmp_path, clock, create=False)
    first.acquire("job", "job-1", "node-1", lease_seconds=10)

    with pytest.raises(OwnershipConflict, match="already owned"):
        second.acquire("job", "job-1", "node-2", lease_seconds=10)


def test_renew_is_idempotent_and_survives_restart(tmp_path):
    clock = Clock()
    journal = _journal(tmp_path, clock)
    lease = journal.acquire("attempt", "attempt-1", "node-1", lease_seconds=10)

    renewed = journal.renew(lease, lease_seconds=20)
    assert renewed.token == lease.token
    assert renewed.expires_at > lease.expires_at
    assert journal.renew(lease, lease_seconds=20) == renewed

    reopened = _journal(tmp_path, clock, create=False)
    assert reopened.renew(lease, lease_seconds=20) == renewed
    clock.value = 105.0
    later = reopened.renew(renewed, lease_seconds=20)
    assert later.expires_at > renewed.expires_at


def test_release_is_idempotent_and_epoch_tombstone_survives_restart(tmp_path):
    clock = Clock()
    journal = _journal(tmp_path, clock)
    lease = journal.acquire("approval", "approval-1", "node-1", lease_seconds=10)

    assert journal.release(lease) is True
    assert journal.release(lease) is True
    reopened = _journal(tmp_path, clock, create=False)
    assert reopened.release(lease) is True

    replacement = reopened.acquire(
        "approval", "approval-1", "node-2", lease_seconds=10
    )
    assert replacement.epoch == lease.epoch + 1
    assert reopened.release(lease) is False


def test_expiry_advances_epoch_and_fences_stale_lease_after_restart(tmp_path):
    clock = Clock()
    journal = _journal(tmp_path, clock)
    old = journal.acquire("session", "session-1", "node-1", lease_seconds=5)
    clock.value = 106.0

    reopened = _journal(tmp_path, clock, create=False)
    new = reopened.acquire("session", "session-1", "node-2", lease_seconds=5)

    assert new.epoch == old.epoch + 1
    assert not reopened.validate(old).allowed
    assert reopened.validate(old).reason == "stale_epoch"
    with pytest.raises(OwnershipConflict, match="stale_epoch"):
        reopened.renew(old, lease_seconds=10)
    assert reopened.release(old) is False


def test_schema_version_mismatch_fails_closed(tmp_path):
    path = tmp_path / "ownership.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=99")
    connection.commit()
    connection.close()

    from sonder_runtime.adapters.persistence.sqlite.cluster_ownership import (
        SQLiteClusterOwnershipJournal,
    )

    with pytest.raises(OwnershipError, match="schema"):
        SQLiteClusterOwnershipJournal(path, cluster_id="cluster-a", clock=Clock())


def test_sqlite_journal_does_not_perform_takeover_without_external_proof(tmp_path):
    journal = _journal(tmp_path, Clock())

    with pytest.raises(TakeoverDenied, match="external.*replicated"):
        journal.takeover(object(), new_owner_id="node-2")


def test_capacity_counts_only_live_leases_and_cluster_scope_isolated(tmp_path):
    clock = Clock()
    first = _journal(tmp_path, clock, max_resources=1)
    first.acquire("job", "job-1", "node-1")
    with pytest.raises(OwnershipConflict, match="capacity"):
        first.acquire("job", "job-2", "node-2")

    other_cluster = _journal(tmp_path, clock, create=False, cluster_id="cluster-b")
    assert other_cluster.acquire("job", "job-1", "node-b").cluster_id == "cluster-b"

    clock.value = 131.0
    assert first.acquire("job", "job-2", "node-2").resource_id == "job-2"


def test_missing_journal_cannot_be_adopted(tmp_path):
    from sonder_runtime.adapters.persistence.sqlite.cluster_ownership import (
        SQLiteClusterOwnershipJournal,
    )

    with pytest.raises(OwnershipError, match="missing"):
        SQLiteClusterOwnershipJournal(
            tmp_path / "missing.sqlite",
            cluster_id="cluster-a",
            create=False,
            clock=Clock(),
        )


def test_concurrent_acquire_has_one_serialized_winner(tmp_path):
    clock = Clock()
    journal = _journal(tmp_path, clock)

    def attempt(owner):
        try:
            return "won", journal.acquire("attempt", "attempt-1", owner)
        except OwnershipConflict:
            return "conflict", owner

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, ("node-1", "node-2")))

    assert sorted(outcome[0] for outcome in outcomes) == ["conflict", "won"]
