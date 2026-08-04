"""SPEC-3: LegacyUnitOfWork + LegacyMemoryRepository over memory_store.

The UnitOfWork owns one memory-store connection for a scope and exposes the
memory repository bound to it; the repository faithfully delegates to the root
memory_store / recall modules. These are offline tests (recall degrades to an
empty list without embeddings).
"""
from __future__ import annotations

import pytest

import reward
from sonder_runtime.adapters.legacy.services import (
    LegacyMemoryRepository,
    LegacyUnitOfWork,
)
from sonder_runtime.bootstrap import app as bootstrap_app


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "memory.db")


def test_constructing_uow_opens_no_database(tmp_path):
    db = tmp_path / "memory.db"
    LegacyUnitOfWork(str(db))
    assert not db.exists()  # connection opens only on __enter__


def test_enter_binds_a_memory_repository(db_path):
    with LegacyUnitOfWork(db_path) as uow:
        assert isinstance(uow.memory, LegacyMemoryRepository)
        assert uow.automation is not None
        assert uow.policy is not None
        assert uow.events is not None


def test_facts_round_trip_and_persist_across_scopes(db_path):
    with LegacyUnitOfWork(db_path) as uow:
        uow.memory.add_fact("f1", "proj", "uses ruff and pytest")
        uow.memory.add_fact("f2", "proj", "python 3.11 target")
        facts = uow.memory.facts_for_project("proj")
        assert {f["text"] for f in facts} == {"uses ruff and pytest", "python 3.11 target"}
        assert uow.memory.count_facts("proj") == 2
    # A fresh scope (new connection) sees the persisted rows.
    with LegacyUnitOfWork(db_path) as uow:
        assert uow.memory.count_facts("proj") == 2
        assert uow.memory.facts_for_project("other") == []


def test_log_and_get_interaction(db_path):
    with LegacyUnitOfWork(db_path) as uow:
        uow.memory.log_interaction("i1", "add a test", "", "done", "code")
        row = uow.memory.get_interaction("i1")
        assert row is not None
        assert row["task"] == "add a test"


def test_recall_returns_a_list_offline(db_path):
    with LegacyUnitOfWork(db_path) as uow:
        uow.memory.log_interaction("i1", "sort a list", "", "use sorted()", "code")
        # Embeddings are unavailable offline, so recall degrades to [] — but it
        # must delegate cleanly and return a list, never raise.
        assert isinstance(uow.memory.recall("sort a list", k=2), list)


def test_record_outcome_delegates_and_validates(db_path):
    with LegacyUnitOfWork(db_path) as uow:
        uow.memory.log_interaction("i1", "task", "", "resp", "code")
        # An unsupported signal must surface memory_store's ValueError — proof
        # the call reaches the real function rather than being swallowed.
        with pytest.raises(ValueError):
            uow.memory.record_outcome("i1", "not-a-real-signal", 1.0)
        # A valid good signal at its canonical reward is accepted.
        good = next(s for s in reward.VALID_SIGNALS if reward.is_good(s))
        result = uow.memory.record_outcome("i1", good, reward.score(good))
        assert result is not None


def test_exit_closes_connection_on_clean_and_error_paths(db_path):
    uow = LegacyUnitOfWork(db_path)
    with uow:
        conn = uow._conn
        assert conn is not None
    assert uow._conn is None  # closed on clean exit
    # On an exception the connection is still closed and the error propagates.
    uow2 = LegacyUnitOfWork(db_path)
    with pytest.raises(RuntimeError):
        with uow2:
            assert uow2._conn is not None
            raise RuntimeError("boom")
    assert uow2._conn is None


def test_application_exposes_unit_of_work_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_DB", str(tmp_path / "memory.db"))
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "policy.json"))
    bootstrap_app.reset_for_tests()
    app = bootstrap_app.build_application()
    # The graph exposes a factory (per-transaction), not a singleton.
    with app.unit_of_work() as uow:
        uow.memory.add_fact("f1", "p", "hello")
        assert uow.memory.count_facts("p") == 1
    bootstrap_app.reset_for_tests()
