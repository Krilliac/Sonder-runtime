"""Per-transaction activation cost for the live authoritative fact source.

Every live memory unit of work re-enters ``activate`` under SQLite's writer
lock.  Full per-row journal authentication is linear in the scope, so it must
run only when the source first claims the scope; later transactions keep a
bounded indexed invariant check.
"""
from __future__ import annotations

import time

import pytest

from sonder_runtime.adapters.embeddings import to_blob
from sonder_runtime.adapters.memory_store import connect
from sonder_runtime.adapters.persistence.sqlite import authoritative_memory
from sonder_runtime.adapters.persistence.sqlite.authoritative_memory import (
    SQLiteAuthoritativeFactSource,
)
from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.domain.memory.replication import MemoryReplicationError

_FACTS = 2000
_DIM = 384

# main's per-transaction invariant before this PR: one NOT EXISTS anti-join.
_REFERENCE_QUERY = (
    "SELECT 1 FROM facts AS fact "
    "JOIN memory_authoritative_fact_state AS state "
    "ON state.project=fact.project AND state.fact_id=fact.id "
    "WHERE fact.project=? AND state.source_id=? AND state.tombstoned=0 "
    "AND NOT EXISTS (SELECT 1 FROM memory_replication_log AS journal "
    "WHERE journal.source_id=state.source_id AND journal.project=fact.project "
    "AND journal.entity_kind='fact' AND journal.entity_id=fact.id "
    "AND journal.version=state.version AND journal.operation='upsert') LIMIT 1"
)


def _populated(path, count=_FACTS):
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    connection = connect(path)
    try:
        source.activate(connection)
        vector = to_blob([0.5] * _DIM)
        connection.execute("BEGIN IMMEDIATE")
        for index in range(count):
            source.add_fact(connection, f"fact-{index:05d}", "repo-a", f"fact {index}", vector)
        connection.commit()
    finally:
        connection.close()
    return source


def _count_full_verification(monkeypatch):
    calls = []
    original = authoritative_memory._verify_scoped_journal_evidence

    def counting(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(authoritative_memory, "_verify_scoped_journal_evidence", counting)
    return calls


def test_full_journal_authentication_runs_only_on_first_activation(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    source = _populated(path, count=5)
    calls = _count_full_verification(monkeypatch)
    for _ in range(3):
        with UnitOfWorkAdapter(str(path), authoritative_fact_source=source) as scope:
            assert len(scope.memory.facts_for_project("repo-a")) == 5
    assert calls == []

    fresh = tmp_path / "fresh.db"
    with UnitOfWorkAdapter(str(fresh), authoritative_fact_source=source):
        pass
    assert calls == [True]


@pytest.mark.parametrize("corruption", ["drop_journal", "drop_fact", "wrong_version"])
def test_reentry_still_fails_closed_on_missing_evidence(tmp_path, corruption):
    path = tmp_path / "memory.db"
    source = _populated(path, count=3)
    raw = connect(path)
    try:
        if corruption == "drop_journal":
            raw.execute("DELETE FROM memory_replication_log WHERE entity_id='fact-00001'")
        elif corruption == "drop_fact":
            # Bypass the legacy fence the way a raw SQL writer would.
            raw.execute("DELETE FROM facts WHERE id='fact-00001'")
        else:
            raw.execute(
                "UPDATE memory_authoritative_fact_state SET version=9 WHERE fact_id='fact-00001'"
            )
        raw.commit()
    finally:
        raw.close()
    with pytest.raises(MemoryReplicationError, match="journal evidence|migration"):
        with UnitOfWorkAdapter(str(path), authoritative_fact_source=source):
            pass


def test_reentry_cost_stays_within_a_constant_of_the_reference_query(tmp_path):
    path = tmp_path / "memory.db"
    source = _populated(path)

    probe = connect(path)
    try:
        reference = []
        for _ in range(5):
            started = time.perf_counter()
            probe.execute(_REFERENCE_QUERY, ("repo-a", "node-a")).fetchone()
            reference.append(time.perf_counter() - started)
    finally:
        probe.close()

    observed = []
    for _ in range(5):
        started = time.perf_counter()
        with UnitOfWorkAdapter(str(path), authoritative_fact_source=source):
            pass
        observed.append(time.perf_counter() - started)

    # The floor absorbs connection/schema setup and CI jitter; before the fix
    # a 2000 x 384-dim scope took well over a second per empty transaction.
    bound = max(0.25, 20 * min(reference))
    assert min(observed) < bound, (min(observed), min(reference))
