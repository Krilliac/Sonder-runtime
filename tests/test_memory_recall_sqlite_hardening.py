"""Regression and concurrency tests for Wave 3 memory, recall & SQLite hardening."""
from __future__ import annotations

import array
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import itertools
import math
import sqlite3

import pytest

from sonder_runtime.adapters import embeddings, memory_rerank, memory_search, memory_store
from sonder_runtime.adapters.persistence import (
    checkpoint_store,
    composition_store,
    migrations,
    queued_actions,
    sqlite_factory,
)
from sonder_runtime.adapters.persistence.owned_sqlite import OwnedSQLiteConnections
from sonder_runtime.adapters.persistence.sqlite import (
    authoritative_indexes,
    automation,
    bridge_migration,
    cas,
    loop_state,
    memory_projection,
    memory_replication,
    operations,
    outbox,
    selfmod,
    strategy_memory as sqlite_strategy_memory,
    training,
    verifier_observations,
)
from sonder_runtime.application.context_planner import (
    CONTEXT_SECTIONS,
    ContextPlanner,
    ModelContext,
)
from sonder_runtime.application.memory import (
    hybrid_retrieval,
    memory_policy,
    playbook_context,
    recall_service,
    retrieval_evaluation,
    strategy_memory,
)
from sonder_runtime.application.persistence.outbox_cas import (
    OutboxEvent,
    PersistenceError,
    TransactionNeutralRecord,
)
from sonder_runtime.application.server_core import memory_backfill
from sonder_runtime.application.strategy.tracing import StrategyTraceService
from sonder_runtime.adapters.persistence.sqlite.runtime_checkpoints import (
    SQLiteRuntimeCheckpointRepository,
)
from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.domain.automation.checkpoint import Checkpoint
from sonder_runtime.domain.common.events import DomainEvent
from sonder_runtime.domain.loop_retry_policy import (
    BackoffMetadata,
    ReplayAction,
    RetryClass,
    RetryDecision,
    SideEffectClass,
    SideEffectRequirement,
)
from sonder_runtime.domain.memory.replication import (
    MemoryMutation,
    MemoryReplicationError,
)
from sonder_runtime.domain.strategy.models import (
    FailureClass,
    FailureObservation,
    StrategyAction,
    StrategyAttempt,
    StrategyBudget,
    StrategySignature,
    StrategyUsage,
)


# ---------------------------------------------------------------------------
# 1. sqlite_factory: handle leak on schema failure, stale path switch, cleanup
# ---------------------------------------------------------------------------


def test_sqlite_factory_cached_connection_closes_handle_on_schema_failure(
    tmp_path, monkeypatch,
):
    owner = OwnedSQLiteConnections((tmp_path,), max_connections=1)
    monkeypatch.setattr(sqlite_factory, "owned_sqlite_connect", owner.connect)
    sqlite_factory.close_cached("schema_fail")
    try:
        with pytest.raises(sqlite3.OperationalError):
            sqlite_factory.cached_connection(
                "schema_fail",
                tmp_path / "bad_schema.db",
                schema_sql="CREATE TABLE broken (;",
            )
        assert owner.snapshot().clean
    finally:
        sqlite_factory.close_cached("schema_fail")


def test_sqlite_factory_path_switch_failure_clears_closed_old_cache(
    tmp_path, monkeypatch,
):
    key = "switch_fail"
    sqlite_factory.close_cached(key)
    db1 = tmp_path / "first.db"
    db2 = tmp_path / "second.db"
    try:
        conn1 = sqlite_factory.cached_connection(key, db1)
        conn1.execute("SELECT 1")

        real_connect = sqlite_factory.connect

        def fail_second(path, **kwargs):
            if str(path) == str(db2):
                raise sqlite3.OperationalError("simulated open failure")
            return real_connect(path, **kwargs)

        monkeypatch.setattr(sqlite_factory, "connect", fail_second)
        with pytest.raises(sqlite3.OperationalError):
            sqlite_factory.cached_connection(key, db2)

        # Re-opening db1 must create a fresh usable connection, not return closed conn1.
        conn1_again = sqlite_factory.cached_connection(key, db1)
        assert conn1_again is not conn1
        assert conn1_again.execute("SELECT 1").fetchone()[0] == 1
    finally:
        sqlite_factory.close_cached(key)


def test_sqlite_factory_close_current_thread_closes_all_even_on_error():
    closed: list[str] = []

    class FakeConn:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        def close(self) -> None:
            closed.append(self.name)
            if self.fail:
                raise sqlite3.OperationalError("close failed")

    sqlite_factory.close_cached("cleanup_a")
    sqlite_factory.close_cached("cleanup_b")
    sqlite_factory._thread_local._sqlite_factory_cleanup_a_conn = FakeConn("a", fail=True)
    sqlite_factory._thread_local._sqlite_factory_cleanup_a_path = "a.db"
    sqlite_factory._thread_local._sqlite_factory_cleanup_b_conn = FakeConn("b", fail=False)
    sqlite_factory._thread_local._sqlite_factory_cleanup_b_path = "b.db"

    with pytest.raises(sqlite3.OperationalError, match="close failed"):
        sqlite_factory.close_current_thread()

    assert set(closed) == {"a", "b"}
    assert sqlite_factory._thread_local._sqlite_factory_cleanup_a_conn is None
    assert sqlite_factory._thread_local._sqlite_factory_cleanup_b_conn is None


@pytest.mark.parametrize("bad_timeout", [-1, True, False])
def test_sqlite_factory_rejects_invalid_busy_timeout(bad_timeout):
    with pytest.raises(ValueError, match="busy_timeout_ms"):
        sqlite_factory.connect(":memory:", busy_timeout_ms=bad_timeout)


# ---------------------------------------------------------------------------
# 2. loop_state & cas: transaction atomicity, corrupt JSON, concurrency
# ---------------------------------------------------------------------------


def test_loop_state_append_rolls_back_record_when_outbox_event_conflicts(tmp_path):
    repo = loop_state.SQLiteLoopStateRepository(tmp_path / "loop.db")
    rec1 = TransactionNeutralRecord("agg-1", 0, {"step": 1})
    ev1 = OutboxEvent("ev-dup", "agg-1", "started", 0, {"ok": True}, "2026-09-01T00:00:00Z")
    assert repo.append(rec1, ev1, expected_revision=-1) == rec1

    # Different aggregate reusing the same event_id violates outbox PK; the
    # record write MUST roll back so agg-2 is NOT persisted without its event.
    rec2 = TransactionNeutralRecord("agg-2", 0, {"step": 1})
    ev_conflict = OutboxEvent("ev-dup", "agg-2", "started", 0, {"ok": True}, "2026-09-01T00:00:00Z")
    assert repo.append(rec2, ev_conflict, expected_revision=-1) is None
    assert repo.get("agg-2") is None
    assert len(repo.outbox()) == 1


def test_loop_state_and_cas_reject_corrupt_json_with_persistence_error(tmp_path):
    loop_db = tmp_path / "loop_corrupt.db"
    repo = loop_state.SQLiteLoopStateRepository(loop_db)
    with sqlite3.connect(loop_db) as conn:
        conn.execute(
            "INSERT INTO loop_idempotency_records VALUES ('bad', 0, 1, '{not-json')"
        )
        conn.execute(
            "INSERT INTO loop_outbox_events VALUES ('e1', 'bad', 't', 0, 1, '[]', '2026-09-01T00:00:00Z')"
        )
    with pytest.raises(PersistenceError, match="corrupt"):
        repo.get("bad")
    with pytest.raises(PersistenceError, match="JSON object"):
        repo.outbox()

    cas_db = tmp_path / "cas_corrupt.db"
    cas_repo = cas.SQLiteOutboxCASRepository(cas_db, namespace="mem")
    with sqlite3.connect(cas_db) as conn:
        conn.execute("INSERT INTO mem_records VALUES ('bad', 0, 1, '{broken')")
    with pytest.raises(PersistenceError, match="corrupt"):
        cas_repo.get("bad")


def test_retry_evidence_ledger_concurrent_writes_stay_bounded(tmp_path):
    ledger = loop_state.SQLiteRetryEvidenceLedger(
        tmp_path / "retry.db", max_records=10, clock=lambda: "2026-09-01T00:00:00Z",
    )
    decision = RetryDecision(
        classification=RetryClass.TRANSIENT,
        action=ReplayAction.RETRY,
        attempt_limit=3,
        backoff=BackoffMetadata(base_seconds=0.5, maximum_seconds=5.0),
        side_effect=SideEffectRequirement(
            effect=SideEffectClass.IDEMPOTENT,
            idempotency_key_required=False,
            idempotency_key_present=True,
            reconciliation_required=False,
            requirement="safe",
        ),
        reason="transient",
    )

    def worker(idx: int) -> None:
        ledger.record(f"op-{idx}", decision, attempt=1, failure_code="timeout")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(24)))

    snap = ledger.snapshot()
    assert len(snap) == 10


# ---------------------------------------------------------------------------
# 3. CheckpointStore, composition_store, outbox, domain init pragmas
# ---------------------------------------------------------------------------


def test_checkpoint_store_degrades_corrupt_context_and_clamps_limit(tmp_path):
    db = tmp_path / "cp.db"
    store = checkpoint_store.CheckpointStore(db)
    for step in range(4):
        store.save(Checkpoint(
            session_id="s1", step_index=step, status="running",
            checkpoint_id=f"cp-{step}", context={"step": step},
        ))
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE checkpoints SET context_json='{corrupt' WHERE checkpoint_id='cp-3'"
        )
        conn.execute(
            "UPDATE checkpoints SET context_json='[1,2,3]' WHERE checkpoint_id='cp-2'"
        )
    latest = store.latest("s1")
    assert latest is not None
    assert latest.checkpoint_id == "cp-3"
    assert latest.context == {}
    cp2 = store.get("cp-2")
    assert cp2 is not None and cp2.context == {}
    # Negative limit must clamp to 1, never SQLite LIMIT -1 (unlimited).
    assert len(store.list_checkpoints("s1", limit=-1)) == 1


def test_composition_store_bounds_limits_and_closes_on_setup_failure(
    tmp_path, monkeypatch,
):
    db = tmp_path / "comp.db"
    monkeypatch.setenv("SONDER_COMPOSITION_DB", str(db))
    composition_store.close_current_thread()
    try:
        for i in range(5):
            composition_store.bind("goal", "g1", "task", f"t-{i}")
        assert len(composition_store.active_bindings(limit=-1)) == 1
        assert len(composition_store.lookup_targets("goal", "g1", limit=2)) == 2
        assert len(composition_store.lookup_sources("task", "t-0", limit=1)) == 1
    finally:
        composition_store.close_current_thread()

    # Verify failed setup closes the opened handle cleanly.
    owner = OwnedSQLiteConnections((tmp_path,), max_connections=1)

    def deny_wal(database, **kwargs):
        conn = owner.connect(database, **kwargs)
        conn.set_authorizer(
            lambda action, name, *_args: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_PRAGMA and name == "journal_mode"
                else sqlite3.SQLITE_OK
            )
        )
        return conn

    monkeypatch.setenv("SONDER_COMPOSITION_DB", str(tmp_path / "fail_comp.db"))
    monkeypatch.setattr(composition_store, "owned_sqlite_connect", deny_wal)
    with pytest.raises(sqlite3.DatabaseError):
        composition_store._connection()
    assert owner.snapshot().clean


def test_outbox_dispatcher_does_not_swallow_non_duplicate_integrity_error():
    source_db = sqlite3.connect(":memory:")
    ops_db = sqlite3.connect(":memory:")
    try:
        source_db.executescript(outbox.OUTBOX_DDL)
        ops_db.executescript(outbox.OPERATIONS_EVENT_DDL)
        # Add a CHECK constraint on ops_db that rejects an event_type to
        # simulate a non-duplicate IntegrityError on a malformed row.
        ops_db.execute(
            "CREATE TRIGGER reject_bad_event BEFORE INSERT ON operation_events "
            "WHEN NEW.event_type = 'corrupt.event' BEGIN "
            "SELECT RAISE(ABORT, 'malformed event'); END;"
        )
        outbox.OutboxWriter(source_db).append(
            DomainEvent(
                event_type="corrupt.event",
                aggregate_type="interaction",
                aggregate_id="agg-1",
                sequence=1,
                payload={},
                correlation_id="c1",
            )
        )
        source_db.commit()
        dispatcher = outbox.OutboxDispatcher(source_db, ops_db, domain="memory")
        with pytest.raises(sqlite3.IntegrityError):
            dispatcher.dispatch_batch()
        # The failed row must remain unpublished (not silently dropped!).
        pub = source_db.execute("SELECT published_at FROM outbox_events").fetchone()[0]
        assert pub is None
        with pytest.raises(ValueError, match="positive integer"):
            dispatcher.dispatch_batch(limit=0)
        with pytest.raises(ValueError, match="positive integer"):
            dispatcher.dispatch_batch(limit=True)
    finally:
        source_db.close()
        ops_db.close()


@pytest.mark.parametrize(
    "module,fn_name",
    [
        (automation, "init_automation_db"),
        (operations, "init_operations_db"),
        (training, "init_training_db"),
        (selfmod, "init_selfmod_db"),
    ],
)
def test_domain_db_inits_set_busy_timeout_before_wal_and_close_on_error(
    tmp_path, monkeypatch, module, fn_name,
):
    executed: list[str] = []
    closed = []

    class FakeConn:
        def execute(self, sql: str):
            executed.append(sql)
            if "journal_mode=WAL" in sql:
                raise sqlite3.OperationalError("wal denied")

        def executescript(self, _sql: str):
            pass

        def close(self):
            closed.append(True)

    monkeypatch.setattr(module, "owned_sqlite_connect", lambda *_a, **_kw: FakeConn())
    with pytest.raises(sqlite3.OperationalError, match="wal denied"):
        getattr(module, fn_name)(tmp_path / "x.db")
    assert executed[0] == "PRAGMA busy_timeout=5000"
    assert executed[1] == "PRAGMA journal_mode=WAL"
    assert closed == [True]


def test_migrations_open_connection_closes_on_pragma_failure_and_validates_timeout(
    tmp_path, monkeypatch,
):
    with pytest.raises(ValueError, match="busy_timeout_ms"):
        migrations.open_connection(str(tmp_path / "m.db"), busy_timeout_ms=-5)
    closed = []

    class FakeConn:
        def execute(self, sql: str):
            if "foreign_keys" in sql:
                raise sqlite3.OperationalError("fk failed")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(migrations, "owned_sqlite_connect", lambda *_a, **_kw: FakeConn())
    with pytest.raises(sqlite3.OperationalError, match="fk failed"):
        migrations.open_connection(str(tmp_path / "m.db"))
    assert closed == [True]


def test_bridge_migration_closes_connections_on_non_hook_failure(tmp_path, monkeypatch):
    owner = OwnedSQLiteConnections((tmp_path,), max_connections=32)
    for mod in (bridge_migration, automation, operations, selfmod, training):
        monkeypatch.setattr(mod, "owned_sqlite_connect", owner.connect)
    monkeypatch.setattr(
        bridge_migration,
        "memory_epoch",
        lambda _conn: (_ for _ in ()).throw(RuntimeError("boom mid-migration")),
    )
    with pytest.raises(RuntimeError, match="boom mid-migration"):
        bridge_migration.run_bridge_migration(tmp_path)
    assert owner.snapshot().clean


def test_queued_actions_connect_closes_on_setup_failure(tmp_path, monkeypatch):
    closed = []

    class FakeConn:
        row_factory = None

        def execute(self, sql: str):
            if "foreign_keys" in sql:
                raise sqlite3.OperationalError("boom")

        def executescript(self, _sql: str):
            pass

        def close(self):
            closed.append(True)

    monkeypatch.setattr(queued_actions, "owned_sqlite_connect", lambda *_a, **_kw: FakeConn())
    with pytest.raises(sqlite3.OperationalError, match="boom"):
        queued_actions.connect(":memory:")
    assert closed == [True]


# ---------------------------------------------------------------------------
# 4. Replication prune_before >250 entities & corrupt JSON handling
# ---------------------------------------------------------------------------


def _fact_mutation(seq: int, entity_id: str, version: int, *, operation: str = "upsert"):
    return MemoryMutation(
        source_id="cluster-a",
        source_epoch=1,
        sequence=seq,
        entity_kind="fact",
        entity_id=entity_id,
        version=version,
        operation=operation,
        project="proj",
        payload={} if operation == "delete" else {"text": f"{entity_id}-v{version}"},
        recorded_at="2026-09-05T12:00:00+00:00",
    )


def test_replication_prune_before_handles_many_entities_without_sqlite_expr_limit(tmp_path):
    """Previously crashed SQLite at ~250 distinct entities with expression-tree overflow."""
    journal = memory_replication.SQLiteMemoryReplicationJournal(
        tmp_path / "repl.db", source_id="cluster-a",
    )
    n_entities = 350
    v1_records = tuple(_fact_mutation(i + 1, f"fact-{i:04d}", 1) for i in range(n_entities))
    assert journal.append(v1_records) == n_entities
    # Supersede all 350 facts (300 upserts + 50 tombstones).
    v2_records = tuple(
        _fact_mutation(
            n_entities + i + 1,
            f"fact-{i:04d}",
            2,
            operation="delete" if i >= 300 else "upsert",
        )
        for i in range(n_entities)
    )
    assert journal.append(v2_records) == n_entities

    # Prune everything strictly before seq 701 while retaining tombstones.
    pruned = journal.prune_before(n_entities * 2 + 1, retain_tombstones=True, limit=1024)
    assert pruned == n_entities  # all 350 v1 rows pruned
    assert len(journal.current_records(limit=1024)) == 300
    assert len(journal.tombstones(limit=1024)) == 50

    # Now prune tombstones too.
    pruned_tombstones = journal.prune_before(
        n_entities * 2 + 1, retain_tombstones=False, limit=1024,
    )
    assert pruned_tombstones == 50
    assert len(journal.current_records(limit=1024)) == 300
    assert journal.tombstones(limit=1024) == ()


def test_replication_projection_and_index_rebuild_handle_corrupt_json(tmp_path):
    db = tmp_path / "corrupt_repl.db"
    journal = memory_replication.SQLiteMemoryReplicationJournal(db, source_id="cluster-a")
    journal.append((_fact_mutation(1, "f1", 1),))
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE memory_replication_log SET payload_json='{broken'")
    with pytest.raises(MemoryReplicationError, match="malformed JSON"):
        journal.export()

    # Authoritative index rebuild wraps corrupt journal JSON in MemoryReplicationError.
    mem_conn = memory_store.connect(":memory:")
    try:
        mem_conn.execute(
            "INSERT INTO memory_replication_log"
            "(source_id,source_epoch,sequence,entity_kind,entity_id,version,operation,project,payload_json,recorded_at,digest) "
            "VALUES ('s1',1,1,'fact','f1',1,'upsert','proj','{bad','2026-09-05T12:00:00+00:00','0')"
        )
        mem_conn.commit()
        with pytest.raises(MemoryReplicationError, match="malformed journal record"):
            authoritative_indexes.rebuild_authoritative_fact_indexes(mem_conn)

        # Projection rebuild wraps corrupt JSON in MemoryProjectionError.
        proj = memory_projection.SQLiteMemoryReplicationProjection(mem_conn)
        mem_conn.execute(
            "INSERT INTO memory_projection_state"
            "(source_id,source_epoch,sequence,entity_kind,entity_id,version,operation,project,payload_json,recorded_at,digest) "
            "VALUES ('s1',1,1,'fact','f1',1,'upsert','proj','{bad','2026-09-05T12:00:00+00:00','0')"
        )
        mem_conn.commit()
        with pytest.raises(memory_projection.MemoryProjectionError, match="malformed JSON"):
            proj.rebuild(source_id="s1")
        with pytest.raises(memory_projection.MemoryProjectionError, match="malformed JSON"):
            proj.current_records(source_id="s1")
    finally:
        mem_conn.close()


# ---------------------------------------------------------------------------
# 5. memory_store: atomicity, chunked task delete, race-free deps, bounded history
# ---------------------------------------------------------------------------


def test_memory_store_delete_interaction_and_tombstone_lesson_are_atomic():
    conn = memory_store.connect(":memory:")
    try:
        memory_store.log_interaction(conn, "i1", "task", "", "resp", "code")
        memory_store.record_outcome_row(conn, "i1", "tests_passed", 1.0, source="caller")
        memory_store.add_lesson(conn, "l1", "lesson text", None, "i1")

        # Install a trigger that aborts DELETE on interactions so we prove
        # earlier DELETEs on lessons/outcomes roll back atomically!
        conn.execute(
            "CREATE TRIGGER abort_del_interaction BEFORE DELETE ON interactions "
            "BEGIN SELECT RAISE(ABORT, 'abort interaction delete'); END;"
        )
        with pytest.raises(sqlite3.IntegrityError, match="abort interaction delete"):
            memory_store.delete_interaction(conn, "i1")
        assert memory_store.get_lesson_text(conn, "l1") == "lesson text"
        assert memory_store.get_interaction(conn, "i1") is not None

        # Install a trigger aborting DELETE on lessons to prove tombstone_lesson
        # rolls back the tombstone insert atomically!
        conn.execute(
            "CREATE TRIGGER abort_del_lesson BEFORE DELETE ON lessons "
            "BEGIN SELECT RAISE(ABORT, 'abort lesson delete'); END;"
        )
        with pytest.raises(sqlite3.IntegrityError, match="abort lesson delete"):
            memory_store.tombstone_lesson(conn, "l1")
        assert not memory_store.lesson_text_tombstoned(conn, "lesson text")

        # commit=False outside a transaction keeps the mutation uncommitted for caller rollback.
        conn.execute("DROP TRIGGER abort_del_lesson")
        assert memory_store.tombstone_lesson(conn, "l1", commit=False) is True
        assert conn.in_transaction is True
        conn.rollback()
        assert memory_store.get_lesson_text(conn, "l1") == "lesson text"
        assert not memory_store.lesson_text_tombstoned(conn, "lesson text")
    finally:
        conn.close()


def test_memory_store_delete_task_handles_many_children_without_param_overflow():
    conn = memory_store.connect(":memory:")
    try:
        parent = memory_store.create_task(conn, "parent")
        for i in range(550):
            memory_store.create_task(conn, f"child-{i}", parent_id=parent["id"])
        result = memory_store.delete_task(conn, parent["id"])
        assert result["children_removed"] == 550
        assert memory_store.get_task(conn, parent["id"]) is None
    finally:
        conn.close()


def test_memory_store_concurrent_opposing_task_deps_never_create_cycle(tmp_path):
    db = tmp_path / "deps.db"
    setup = memory_store.connect(str(db))
    t_a = memory_store.create_task(setup, "task-a")["id"]
    t_b = memory_store.create_task(setup, "task-b")["id"]
    setup.close()

    def add_edge(pair: tuple[str, str]) -> str:
        conn = memory_store.connect(str(db))
        try:
            memory_store.add_task_dep(conn, pair[0], pair[1])
            return "added"
        except ValueError as exc:
            if "cycle" in str(exc):
                return "cycle"
            raise
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(add_edge, [(t_a, t_b), (t_b, t_a)]))
    assert sorted(outcomes) == ["added", "cycle"]


def test_memory_store_session_history_bounded_and_candidate_page_nan_guard():
    conn = memory_store.connect(":memory:")
    try:
        for idx in range(20):
            memory_store.log_interaction(
                conn, f"i-{idx:02d}", f"task-{idx}", "", f"resp-{idx}", "code",
                session_id="sess-1",
            )
        hist = memory_store.session_history(conn, "sess-1", max_turns=3)
        assert hist == [
            ("task-17", "resp-17"),
            ("task-18", "resp-18"),
            ("task-19", "resp-19"),
        ]
        for bad_time in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError, match="finite"):
                memory_store.good_interaction_candidate_page(conn, time_limit_s=bad_time)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. Recall, MMR rerank, memory_search, hybrid_retrieval, retrieval_eval,
#    strategy_memory N+1, playbook_context, memory_backfill
# ---------------------------------------------------------------------------


def test_mmr_from_blobs_skips_corrupt_and_nonfinite_blobs():
    query = [1.0, 0.0, 0.0]
    nan_blob = array.array("f", [float("nan"), 1.0, 0.0]).tobytes()
    zero_blob = array.array("f", [0.0, 0.0, 0.0]).tobytes()
    pairs = [
        ("corrupt-len", b"\x01\x02\x03"),
        ("corrupt-nan", nan_blob),
        ("corrupt-zero", zero_blob),
        ("good-a", embeddings.to_blob([0.9, 0.1, 0.0])),
        ("good-b", embeddings.to_blob([0.1, 0.9, 0.0])),
    ]
    out = memory_rerank.mmr_from_blobs(query, pairs, k=2, lambda_mult=0.5)
    assert out == ["good-a", "good-b"]


def test_memory_search_escapes_backslashes_and_bounds_matcher_cache():
    conn = memory_store.connect(":memory:")
    try:
        memory_store.add_fact(conn, "f1", "proj", r"path is C:\Users\dev\app_1%", None)
        report = memory_search.search_memory(conn, memory_store, r"C:\Users\dev\app_1%", 10)
        assert "f1" in report
        with pytest.raises(ValueError, match="positive integer"):
            memory_search.search_memory(conn, memory_store, "path", True)  # type: ignore[arg-type]
    finally:
        conn.close()

    matcher = memory_search.bound_project_matcher(r"C:\repo\alpha")
    for idx in range(memory_search._MAX_MATCHER_CACHE + 500):
        assert matcher(f"C:\\other\\proj-{idx}") is False
    # Inspect closure's bounded cache size.
    seen_dict = next(
        cell.cell_contents for cell in (matcher.__closure__ or ())
        if isinstance(cell.cell_contents, dict)
    )
    assert len(seen_dict) <= memory_search._MAX_MATCHER_CACHE


def test_recall_service_skips_corrupt_candidates_and_nonfinite_sim():
    class CorruptStore:
        def lexical_search(self, query, *, k, project, exclude_session):
            return [{"text": "lexical fallback"}]

        def semantic_candidates(self, *, k, project, exclude_session, embedding_model, embedding_revision):
            return [
                "not-a-dict",  # type: ignore[list-item]
                {"id": "no-text", "embedding": [1.0, 0.0]},  # type: ignore[typeddict-item]
                {"id": "int-vec", "text": "bad", "embedding": 12345},  # type: ignore[typeddict-item]
                {"id": "str-vec", "text": "bad", "embedding": "abcd"},  # type: ignore[typeddict-item]
                {"id": "nan-vec", "text": "bad", "embedding": [float("nan"), 1.0]},
                {"id": "good", "text": "good memory", "embedding": [1.0, 0.0]},
            ]

    class FixedEmbed:
        def embed(self, _text: str):
            return [1.0, 0.0]

    svc = recall_service.RecallService(CorruptStore(), embed=FixedEmbed())
    assert svc.recall("query", k=2, min_sim=0.5) == ["good memory"]


def test_hybrid_retrieval_nan_semantic_score_and_pre_1970_timestamp():
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    pre_epoch = datetime(1960, 1, 1, tzinfo=timezone.utc)
    rows = [
        hybrid_retrieval.MemoryCandidate(
            "nan-score", "release lock", memory_policy.MemoryClass.WORKING,
            created_at=pre_epoch, confidence=0.5, semantic_score=float("nan"),
        ),
        hybrid_retrieval.MemoryCandidate(
            "real-score", "release lock", memory_policy.MemoryClass.WORKING,
            created_at=pre_epoch, confidence=0.5, semantic_score=0.2,
        ),
    ]
    ranked = hybrid_retrieval.HybridMemoryRetriever().retrieve(
        rows, hybrid_retrieval.RetrievalQuery("release lock", limit=2, at=now),
    )
    assert [item.candidate.memory_id for item in ranked] == ["real-score", "nan-score"]
    assert math.isfinite(ranked[1].score)
    assert ranked[0].score > ranked[1].score


def test_memory_policy_rejects_bool_and_non_numeric_confidence():
    w = memory_policy.evaluate_write(
        memory_policy.MemoryClass.WORKING, confidence=True,  # type: ignore[arg-type]
    )
    assert not w.allowed
    r = memory_policy.evaluate_retrieval(
        "m1", memory_policy.MemoryClass.WORKING,
        confidence=None,  # type: ignore[arg-type]
        freshness=True,  # type: ignore[arg-type]
    )
    assert not r.included
    assert "confidence is invalid" in r.exclusion_reasons
    assert "freshness is invalid" in r.exclusion_reasons


def test_retrieval_evaluation_bounds_streaming_dedups_contradictions_and_checks_nan():
    infinite = (
        retrieval_evaluation.RetrievalLabel(f"q-{i}", frozenset({"a"}))
        for i in itertools.count()
    )
    with pytest.raises(ValueError, match="too many retrieval evaluation cases"):
        retrieval_evaluation.evaluate_retrieval(infinite)

    with pytest.raises(ValueError, match="k must be"):
        retrieval_evaluation.evaluate_retrieval([], k=True)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="latency_ms"):
        retrieval_evaluation.evaluate_retrieval([
            retrieval_evaluation.RetrievalLabel("q1", frozenset(), latency_ms=float("nan")),
        ])

    # Duplicate contradictory memory_id returned twice must be counted ONCE.
    dup_case = retrieval_evaluation.RetrievalLabel(
        query_id="q1",
        relevant_ids=frozenset({"good"}),
        retrieved=(
            retrieval_evaluation.RetrievedMemory("bad", is_contradictory=True),
            retrieval_evaluation.RetrievedMemory("bad", is_contradictory=True),
            retrieval_evaluation.RetrievedMemory("good"),
        ),
    )
    metrics = retrieval_evaluation.evaluate_retrieval([dup_case])
    assert metrics.evaluated_results == 2
    assert metrics.contradiction_rate == pytest.approx(0.5)


def test_strategy_memory_avoids_n_plus_1_per_signature_and_handles_corrupt_json(tmp_path):
    checkpoint = SQLiteRuntimeCheckpointRepository(tmp_path / "rt.sqlite", seal_key=b"k" * 32)
    trace = StrategyTraceService(checkpoint)
    mem_db = tmp_path / "mem.sqlite"
    svc = strategy_memory.StrategyMemoryService(trace, lambda: UnitOfWorkAdapter(str(mem_db)))

    sig = StrategySignature("patch", "a" * 64, ("src/mod.py",), "b" * 64, "r", "pytest")
    for idx in range(5):
        att = StrategyAttempt(
            f"run-{idx}", "attempt-1", sig, "failed",
            failure=FailureObservation(FailureClass.TEST_FAILURE, evidence_digest="f" * 64),
            usage=StrategyUsage(attempts=1, model_calls=1, verifier_calls=1),
        )
        trace.record(att, budget=StrategyBudget(), available_actions=(StrategyAction.REPAIR,))
        svc.observe_recorded(f"run-{idx}", "attempt-1", project_scope="proj-a")

    by_sig_calls = 0
    failed_single_calls = 0
    failed_batch_calls = 0
    orig_by_sig = sqlite_strategy_memory.SQLiteStrategyExperienceRepository.by_signature
    orig_failed = sqlite_strategy_memory.SQLiteStrategyExperienceRepository.failed_reuses
    orig_batch = sqlite_strategy_memory.SQLiteStrategyExperienceRepository.failed_reuses_batch

    def tracked_by_sig(self, *args, **kwargs):
        nonlocal by_sig_calls
        by_sig_calls += 1
        return orig_by_sig(self, *args, **kwargs)

    def tracked_failed(self, *args, **kwargs):
        nonlocal failed_single_calls
        failed_single_calls += 1
        return orig_failed(self, *args, **kwargs)

    def tracked_batch(self, *args, **kwargs):
        nonlocal failed_batch_calls
        failed_batch_calls += 1
        return orig_batch(self, *args, **kwargs)

    sqlite_strategy_memory.SQLiteStrategyExperienceRepository.by_signature = tracked_by_sig  # type: ignore[method-assign]
    sqlite_strategy_memory.SQLiteStrategyExperienceRepository.failed_reuses = tracked_failed  # type: ignore[method-assign]
    sqlite_strategy_memory.SQLiteStrategyExperienceRepository.failed_reuses_batch = tracked_batch  # type: ignore[method-assign]
    try:
        budgets = dict.fromkeys(CONTEXT_SECTIONS, 0)
        budgets["memories"] = 240
        plan = ContextPlanner().plan(ModelContext("m", 512, 64), {"memories": 240}, budgets)
        selected = svc.select_for_attempt(
            "run-recover", "attempt-1", project_scope="proj-a", plan=plan,
            failure_class=FailureClass.TEST_FAILURE, family="patch",
        )
        assert len(selected.references) == 5
        # All 5 rows share 1 signature -> by_signature & failed_reuses_batch called ONCE!
        assert by_sig_calls == 1
        assert failed_batch_calls == 1
        assert failed_single_calls == 0
    finally:
        sqlite_strategy_memory.SQLiteStrategyExperienceRepository.by_signature = orig_by_sig  # type: ignore[method-assign]
        sqlite_strategy_memory.SQLiteStrategyExperienceRepository.failed_reuses = orig_failed  # type: ignore[method-assign]
        sqlite_strategy_memory.SQLiteStrategyExperienceRepository.failed_reuses_batch = orig_batch  # type: ignore[method-assign]

    # Corrupt JSON in strategy_experience and verifier_learning_observations raises ValueError.
    with sqlite3.connect(mem_db) as conn:
        conn.execute("DROP TRIGGER IF EXISTS strategy_experience_immutable_update")
        conn.execute("UPDATE strategy_experience SET usage_json='{bad'")
    with UnitOfWorkAdapter(str(mem_db)) as unit:
        with pytest.raises(ValueError, match="corrupt strategy experience JSON"):
            unit.strategy_experiences.relevant(
                strategy_memory._scope("proj-a"),
                failure_class="", family="", verifier_fingerprint="",
                language="", subsystem_digest="", outcome="", limit=5,
            )

    obs_conn = sqlite3.connect(":memory:")
    try:
        obs_repo = verifier_observations.SQLiteVerifierObservationRepository(obs_conn)
        obs_conn.execute(
            "INSERT INTO verifier_learning_observations(observation_id, receipt_id, receipt_json, observation_json) "
            "VALUES ('o1', 'r1', '[]', '{}')"
        )
        with pytest.raises(ValueError, match="corrupt verifier observation JSON"):
            obs_repo.get("o1")
        with pytest.raises(ValueError, match="corrupt verifier observation JSON"):
            obs_repo.get_many(("o1",))
    finally:
        obs_conn.close()


def test_playbook_context_skips_malformed_entries_cleanly():
    class CorruptReader:
        def approved_index(self) -> str:
            return "- [Deploy](deploy.md) — ops — open when: deploy service\n"

        def read(self, topic: str, approved_only: bool = True):
            return [
                "not-a-dict",
                None,
                {"status": "approved", "id": "e1", "title": "Safe", "body": "deploy service safely"},
            ]

    ctx = playbook_context.PlaybookContext(lambda: CorruptReader())
    sel = ctx.select("please deploy service now", session_id="s1")
    assert len(sel.topics) == 1
    assert sel.topics[0]["entry_ids"] == ["e1"]


def test_memory_backfill_no_connection_leak_on_revision_error_and_handles_sqlite_row_error():
    opened = []
    closed = []

    class FakeConn:
        in_transaction = False

        def rollback(self):
            self.in_transaction = False

        def close(self):
            closed.append(True)

    def open_db():
        opened.append(True)
        return FakeConn()

    # 1. If current_revision raises, open_db is not even called -> zero leaked connections.
    with pytest.raises(RuntimeError, match="ollama down"):
        memory_backfill.run_embedding_backfill(
            memory_backfill.LESSON_BACKFILL_SPEC,
            limit=10,
            apply=False,
            approved_model="nomic-embed-text",
            approved_base="http://127.0.0.1:11434",
            expected_dimension=2,
            is_cloud_model=lambda _m: False,
            endpoint_is_loopback=lambda _b: True,
            canonical_model_name=lambda m: m,
            current_revision=lambda **_kw: (_ for _ in ()).throw(RuntimeError("ollama down")),
            embed_fn=lambda *_a, **_kw: [1.0, 0.0],
            valid_vector=lambda v: bool(v),
            to_blob=lambda _v: b"\x00\x00\x80?\x00\x00\x00\x00",
            open_db=open_db,
            select_rows=lambda *_a, **_kw: [],
            refresh_row=lambda *_a, **_kw: True,
            count_remaining=lambda *_a, **_kw: 0,
        )
    assert opened == []
    assert closed == []

    # 2. Invalid limits rejected cleanly.
    for bad_limit in (0, -5, True):
        with pytest.raises(ValueError, match="backfill limit"):
            memory_backfill.run_embedding_backfill(
                memory_backfill.LESSON_BACKFILL_SPEC,
                limit=bad_limit,  # type: ignore[arg-type]
                apply=False,
                approved_model="nomic-embed-text",
                approved_base="http://127.0.0.1:11434",
                expected_dimension=2,
                is_cloud_model=lambda _m: False,
                endpoint_is_loopback=lambda _b: True,
                canonical_model_name=lambda m: m,
                current_revision=lambda **_kw: "rev1",
                embed_fn=lambda *_a, **_kw: [1.0, 0.0],
                valid_vector=lambda v: bool(v),
                to_blob=lambda _v: b"\x00\x00\x80?\x00\x00\x00\x00",
                open_db=open_db,
                select_rows=lambda *_a, **_kw: [],
                refresh_row=lambda *_a, **_kw: True,
                count_remaining=lambda *_a, **_kw: 0,
            )

    # 3. Per-row sqlite3.OperationalError records failed ID and continues batch.
    def flaky_refresh(conn, row_id, *_a, **_kw):
        if row_id == "r1":
            conn.in_transaction = True
            raise sqlite3.OperationalError("database is locked")
        return True

    res = memory_backfill.run_embedding_backfill(
        memory_backfill.LESSON_BACKFILL_SPEC,
        limit=10_000,  # clamped to MAX_BACKFILL_LIMIT
        apply=True,
        approved_model="nomic-embed-text",
        approved_base="http://127.0.0.1:11434",
        expected_dimension=2,
        is_cloud_model=lambda _m: False,
        endpoint_is_loopback=lambda _b: True,
        canonical_model_name=lambda m: m,
        current_revision=lambda **_kw: "rev1",
        embed_fn=lambda *_a, **_kw: [1.0, 0.0],
        valid_vector=lambda v: bool(v),
        to_blob=lambda _v: b"\x00\x00\x80?\x00\x00\x00\x00",
        open_db=open_db,
        select_rows=lambda _c, _m, *, limit, **_kw: (
            [{"id": "r1", "text": "one"}, {"id": "r2", "text": "two"}]
            if limit == memory_backfill.MAX_BACKFILL_LIMIT
            else []
        ),
        refresh_row=flaky_refresh,
        count_remaining=lambda *_a, **_kw: 1,
    )
    assert "updated: 1 | conflicted: 0 | failed: 1 | remaining: 1" in res.report
    assert "failed IDs: r1" in res.report
    assert len(opened) == 1 and len(closed) == 1
