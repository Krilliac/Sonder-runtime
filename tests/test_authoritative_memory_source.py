"""Atomic source-side evidence for the deliberately narrow fact write set."""
from __future__ import annotations

import pytest

from sonder_runtime.adapters.embeddings import from_blob, to_blob
from sonder_runtime.adapters.memory_store import connect, facts_for_project
from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.adapters.persistence.sqlite.authoritative_memory import (
    AuthoritativeFactMetadata,
    SQLiteAuthoritativeFactSource,
)
from sonder_runtime.adapters.persistence.sqlite.memory_replication import (
    SQLiteMemoryReplicationJournal,
)
from sonder_runtime.adapters.persistence.sqlite.memory_projection import (
    SQLiteMemoryReplicationProjection,
)
from sonder_runtime.domain.memory.replication import MemoryReplicationError
from sonder_runtime.bootstrap.app import build_application
from sonder_runtime.platform.config import Secrets, SonderConfig
from sonder_runtime.platform.memory_replication_config import (
    MemoryReplicationConfig, MemoryReplicationPeerConfig,
)


def _live_replication_config() -> SonderConfig:
    return SonderConfig(
        secrets=Secrets(
            api_key="api-" + "a" * 32,
            artifact_transfer_key="artifact-" + "b" * 32,
            auth_secret="auth-" + "c" * 32,
            memory_replication_key="replication-" + "d" * 48,
            memory_replication_state_integrity_key="state-" + "e" * 48,
        ),
        memory_replication=MemoryReplicationConfig(
            enabled=True,
            local_node_id="node-a",
            project_scope="repo-a",
            peers=(MemoryReplicationPeerConfig(
                node_id="node-b", project_scope="repo-a",
                origin="https://node-b.example:8443",
            ),),
        ),
    )


def test_windows_scope_is_accepted_as_opaque_exact_identity(tmp_path):
    """Backslashes are valid scope text; slash variants remain distinct."""
    path = tmp_path / "windows-scope.db"
    windows_scope = r"C:\Users\owner\workspace"
    slash_variant = "C:/Users/owner/workspace"
    source = SQLiteAuthoritativeFactSource("node-a", project_scope=windows_scope)
    connection = connect(path)
    try:
        source.activate(connection)
        source.add_fact(connection, "fact-1", windows_scope, "accepted")
        assert facts_for_project(connection, windows_scope)[0]["text"] == "accepted"
        with pytest.raises(MemoryReplicationError, match="scope"):
            source.add_fact(connection, "fact-2", slash_variant, "must refuse")
        assert facts_for_project(connection, slash_variant) == []
    finally:
        connection.close()


def test_live_application_composes_authoritative_fact_write_and_restart(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    monkeypatch.setenv("SONDER_DB", str(path))
    config = _live_replication_config()
    first = build_application(config=config)
    try:
        with first.unit_of_work() as scope:
            scope.memory.add_fact("fact-1", "repo-a", "first")
        with pytest.raises(MemoryReplicationError, match="scope"):
            with first.unit_of_work() as scope:
                scope.memory.add_fact("wrong-project", "repo-b", "not admitted")
        assert first.memory_replication._database_path_for_operation() == path
    finally:
        first.close_providers()

    restarted = build_application(config=config)
    try:
        with restarted.unit_of_work() as scope:
            assert scope.memory.facts_for_project("repo-a")[0]["text"] == "first"
            scope.memory.add_fact("fact-2", "repo-a", "second")
    finally:
        restarted.close_providers()

    journal = SQLiteMemoryReplicationJournal(
        path, source_id="node-a", project_scope="repo-a",
    )
    try:
        assert [(record.sequence, record.entity_id) for record in journal.export(limit=10).records] == [
            (1, "fact-1"), (2, "fact-2"),
        ]
    finally:
        journal.close()


def test_live_composition_preserves_scoped_supersession_after_restart(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    monkeypatch.setenv("SONDER_DB", str(path))
    config = _live_replication_config()
    application = build_application(config=config)
    try:
        with application.unit_of_work() as scope:
            scope.memory.add_fact("old", "repo-a", "old policy", metadata=AuthoritativeFactMetadata(
                entities=("parser",), valid_from="2026-01-01T00:00:00Z",
                provenance=("review:old",),
            ))
            scope.memory.add_fact("new", "repo-a", "new policy", metadata=AuthoritativeFactMetadata(
                entities=("parser",), supersedes="old",
                valid_from="2026-02-01T00:00:00Z", provenance=("review:new",),
            ))
    finally:
        application.close_providers()

    restarted = build_application(config=config)
    try:
        with restarted.unit_of_work() as scope:
            january = scope.memory.entities_for_project("repo-a", now="2026-01-15T00:00:00Z")
            february = scope.memory.entities_for_project("repo-a", now="2026-02-15T00:00:00Z")
            assert [row["fact_id"] for row in january] == ["old"]
            assert [row["fact_id"] for row in february] == ["new"]
            with pytest.raises(MemoryReplicationError, match="scope"):
                scope.memory.entities_for_project("repo-b")
            assert scope.memory.rebuild_authoritative_indexes(project="repo-a") == 2
            assert [row["fact_id"] for row in scope.memory.entities_for_project(
                "repo-a", now="2026-02-15T00:00:00Z",
            )] == ["new"]
    finally:
        restarted.close_providers()


def test_live_authority_fences_legacy_fact_helpers_for_the_active_scope(tmp_path):
    from sonder_runtime.adapters import memory_store

    path = tmp_path / "memory.db"
    application = build_application(config=_live_replication_config())
    try:
        # Entering the real application UoW publishes the authority marker,
        # even before the first fact write.
        with application.unit_of_work(db_path=str(path)):
            pass
    finally:
        application.close_providers()

    connection = connect(path)
    try:
        with pytest.raises(MemoryReplicationError, match="legacy fact writes"):
            memory_store.add_fact(connection, "legacy", "repo-a", "bypass")
        with pytest.raises(MemoryReplicationError, match="legacy fact writes"):
            memory_store.delete_fact(connection, "legacy", "repo-a")
    finally:
        connection.close()


@pytest.mark.parametrize("operation", ["upsert", "delete"])
def test_active_authoritative_scope_rejects_direct_other_source_mutations(
    tmp_path, operation,
):
    path = tmp_path / f"other-source-{operation}.db"
    connection = connect(path)
    owner = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    owner.activate(connection)
    owner.add_fact(connection, "fact-1", "repo-a", "owned value")
    outsider = SQLiteAuthoritativeFactSource("node-b", project_scope="repo-a")

    with pytest.raises(MemoryReplicationError, match="already owned"):
        if operation == "upsert":
            outsider.upsert_fact(connection, "fact-1", "repo-a", "bypass")
        else:
            outsider.delete_fact(connection, "fact-1", "repo-a")

    assert facts_for_project(connection, "repo-a")[0]["text"] == "owned value"
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_replication_log"
    ).fetchone()[0] == 1
    assert tuple(connection.execute(
        "SELECT source_id,version,tombstoned FROM memory_authoritative_fact_state "
        "WHERE project=? AND fact_id=?", ("repo-a", "fact-1"),
    ).fetchone()) == ("node-a", 1, 0)
    connection.close()


def test_composed_authoritative_reads_reject_cross_project_scope(tmp_path):
    path = tmp_path / "read-scope.db"
    application = build_application(config=_live_replication_config())
    try:
        with application.unit_of_work(db_path=str(path)) as scope:
            scope.memory.add_fact("fact-1", "repo-a", "scoped value")
        with application.unit_of_work(db_path=str(path)) as scope:
            for read in (
                lambda: scope.memory.facts_for_project("repo-b"),
                lambda: scope.memory.count_facts("repo-b"),
                lambda: scope.memory.entities_for_project("repo-b"),
                lambda: scope.memory.decisions_for_project("repo-b"),
            ):
                with pytest.raises(MemoryReplicationError, match="widen"):
                    read()
    finally:
        application.close_providers()


@pytest.mark.parametrize("stage", ["fact", "state", "journal", "index"])
def test_composed_authoritative_fact_stages_roll_back_after_injected_failure(
    tmp_path, monkeypatch, stage,
):
    from sonder_runtime.adapters.persistence.sqlite import authoritative_memory

    path = tmp_path / f"{stage}.db"
    if stage == "fact":
        monkeypatch.setattr(
            authoritative_memory,
            "_insert_fact_row",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("fact stage failed")
            ),
        )
    elif stage == "state":
        original = authoritative_memory.SQLiteAuthoritativeFactSource._store_state

        def fail_state(self, connection, record):
            original(self, connection, record)
            raise RuntimeError("state stage failed")

        monkeypatch.setattr(
            authoritative_memory.SQLiteAuthoritativeFactSource,
            "_store_state",
            fail_state,
        )
    elif stage == "journal":
        monkeypatch.setattr(
            authoritative_memory,
            "append_memory_mutations_in_transaction",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("journal stage failed")
            ),
        )
    else:
        monkeypatch.setattr(
            authoritative_memory,
            "materialize_authoritative_fact_index",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("index stage failed")
            ),
        )

    application = build_application(config=_live_replication_config())
    try:
        with pytest.raises(RuntimeError, match=f"{stage} stage failed"):
            with application.unit_of_work(db_path=str(path)) as scope:
                scope.memory.add_fact("fact-1", "repo-a", "must roll back")
    finally:
        application.close_providers()

    connection = connect(path)
    try:
        assert facts_for_project(connection, "repo-a") == []
        for table in (
            "memory_authoritative_fact_state",
            "memory_replication_log",
            "memory_authoritative_entity_index",
            "memory_authoritative_decision_index",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        # Activation is durable policy state; only the attempted fact mutation
        # must disappear on rollback.
        assert tuple(connection.execute(
            "SELECT source_id,project_scope FROM memory_authoritative_fact_activation"
        ).fetchone()) == ("node-a", "repo-a")
    finally:
        connection.close()


def test_live_activation_refuses_existing_unjournaled_scoped_facts(tmp_path):
    from sonder_runtime.adapters import memory_store

    path = tmp_path / "memory.db"
    connection = connect(path)
    memory_store.add_fact(connection, "legacy", "repo-a", "existing")
    connection.close()

    application = build_application(config=_live_replication_config())
    try:
        with pytest.raises(MemoryReplicationError, match="authoritative migration"):
            with application.unit_of_work(db_path=str(path)) as scope:
                scope.memory.add_fact("new", "repo-a", "must not mix")
    finally:
        application.close_providers()

    connection = connect(path)
    try:
        assert [row["id"] for row in facts_for_project(connection, "repo-a")] == ["legacy"]
        assert connection.execute("SELECT COUNT(*) FROM memory_replication_log").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_authoritative_fact_activation"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_direct_activation_fails_closed_without_publishing_marker(tmp_path):
    path = tmp_path / "activation-gate.db"
    connection = connect(path)
    try:
        from sonder_runtime.adapters import memory_store
        memory_store.add_fact(connection, "legacy", "repo-a", "requires migration")
        source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
        with pytest.raises(MemoryReplicationError, match="authoritative migration"):
            source.activate(connection)
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_authoritative_fact_activation"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_activation_rejects_state_without_matching_journal_evidence(tmp_path):
    path = tmp_path / "missing-journal.db"
    connection = connect(path)
    try:
        connection.execute(
            "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
            ("fact-1", "repo-a", "state without journal", None),
        )
        connection.execute(
            "INSERT INTO memory_authoritative_fact_state"
            "(project,fact_id,source_id,version,tombstoned) VALUES(?,?,?,?,?)",
            ("repo-a", "fact-1", "node-a", 1, 0),
        )
        connection.commit()
        source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
        with pytest.raises(MemoryReplicationError, match="journal evidence"):
            source.activate(connection)
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_authoritative_fact_activation"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_authoritative_write_does_not_rescan_all_journal_evidence(tmp_path):
    connection = connect(tmp_path / "incremental-write.db")
    statements = []
    connection.set_trace_callback(statements.append)
    try:
        source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
        source.add_fact(connection, "fact-1", "repo-a", "incremental write")
        assert not any(
            statement.casefold().startswith("select")
            and "not exists" in statement.casefold()
            and "memory_replication_log" in statement.casefold()
            for statement in statements
        )
    finally:
        connection.set_trace_callback(None)
        connection.close()


def test_live_source_and_fact_rollback_together_when_journal_fails(tmp_path, monkeypatch):
    from sonder_runtime.adapters.persistence.sqlite import authoritative_memory

    path = tmp_path / "memory.db"
    def reject_journal(*args, **kwargs):
        raise RuntimeError("journal failed")

    monkeypatch.setattr(
        authoritative_memory, "append_memory_mutations_in_transaction", reject_journal,
    )
    application = build_application(config=_live_replication_config())
    try:
        with pytest.raises(RuntimeError, match="journal failed"):
            with application.unit_of_work(db_path=str(path)) as scope:
                scope.memory.add_fact("fact-1", "repo-a", "uncommitted")
    finally:
        application.close_providers()

    connection = connect(path)
    try:
        assert facts_for_project(connection, "repo-a") == []
        assert connection.execute("SELECT COUNT(*) FROM memory_authoritative_fact_state").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_replication_log").fetchone()[0] == 0
    finally:
        connection.close()


def test_authoritative_fact_source_commits_fact_and_journal_record_together(tmp_path):
    """The supported live set is one project-scoped ``fact`` entity kind."""
    path = tmp_path / "memory.db"
    connection = connect(path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")

    record = source.add_fact(
        connection,
        "fact-1",
        "repo-a",
        "Use focused pytest regressions.",
        None,
    )

    assert facts_for_project(connection, "repo-a") == [
        {
            "id": "fact-1",
            "project": "repo-a",
            "text": "Use focused pytest regressions.",
            "embedding": None,
        }
    ]
    assert (
        record.source_id,
        record.source_epoch,
        record.sequence,
        record.entity_kind,
        record.entity_id,
        record.version,
        record.operation,
        record.project,
        dict(record.payload),
    ) == (
        "node-a",
        1,
        1,
        "fact",
        "fact-1",
        1,
        "upsert",
        "repo-a",
        {"text": "Use focused pytest regressions.", "embedding": None},
    )
    connection.close()

    journal = SQLiteMemoryReplicationJournal(
        path,
        source_id="node-a",
        project_scope="repo-a",
    )
    try:
        assert journal.export().records == (record,)
    finally:
        journal.close()


def test_authoritative_fact_source_advances_entity_version_on_explicit_upsert(tmp_path):
    path = tmp_path / "memory.db"
    connection = connect(path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(connection, "fact-1", "repo-a", "first value")

    updated = source.upsert_fact(
        connection,
        "fact-1",
        "repo-a",
        "second value",
    )

    assert updated.sequence == 2
    assert updated.version == 2
    assert updated.operation == "upsert"
    assert facts_for_project(connection, "repo-a")[0]["text"] == "second value"
    connection.close()

    journal = SQLiteMemoryReplicationJournal(
        path,
        source_id="node-a",
        project_scope="repo-a",
    )
    try:
        assert [record.version for record in journal.export(limit=10).records] == [1, 2]
    finally:
        journal.close()


def test_authoritative_fact_source_emits_a_projection_safe_live_embedding(tmp_path):
    path = tmp_path / "memory.db"
    connection = connect(path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")

    record = source.add_fact(
        connection,
        "fact-1",
        "repo-a",
        "embedded fact",
        to_blob([1.0, -0.5]),
    )

    assert list(record.payload["embedding"]) == pytest.approx([1.0, -0.5])
    connection.close()

    journal = SQLiteMemoryReplicationJournal(
        path,
        source_id="node-a",
        project_scope="repo-a",
    )
    target = connect(tmp_path / "target.db")
    try:
        assert SQLiteMemoryReplicationProjection(target).apply(journal.export()) == 1
        assert from_blob(facts_for_project(target, "repo-a")[0]["embedding"]) == pytest.approx(
            [1.0, -0.5]
        )
    finally:
        target.close()
        journal.close()


def test_authoritative_fact_source_deletion_commits_a_tombstone(tmp_path):
    path = tmp_path / "memory.db"
    connection = connect(path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(connection, "fact-1", "repo-a", "obsolete value")

    assert source.delete_fact(connection, "fact-1", "repo-a") is True
    assert facts_for_project(connection, "repo-a") == []
    connection.close()

    journal = SQLiteMemoryReplicationJournal(
        path,
        source_id="node-a",
        project_scope="repo-a",
    )
    try:
        records = journal.export(limit=10).records
        assert [(record.sequence, record.version, record.operation, dict(record.payload)) for record in records] == [
            (1, 1, "upsert", {"text": "obsolete value", "embedding": None}),
            (2, 2, "delete", {}),
        ]
        assert journal.tombstones() == (records[-1],)
    finally:
        journal.close()


def test_authoritative_fact_source_rolls_back_materialized_fact_when_journal_rejects(
    tmp_path,
):
    connection = connect(tmp_path / "memory.db")
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    connection.execute(
        "CREATE TRIGGER reject_authoritative_journal "
        "BEFORE INSERT ON memory_replication_log BEGIN "
        "SELECT RAISE(ABORT, 'journal unavailable'); END"
    )

    with pytest.raises(MemoryReplicationError):
        source.add_fact(connection, "fact-1", "repo-a", "must not persist")

    assert facts_for_project(connection, "repo-a") == []
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_replication_log"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_authoritative_fact_state"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_replication_meta"
    ).fetchone()[0] == 0
    connection.close()


def test_unit_of_work_routes_an_explicit_authoritative_fact_source(tmp_path):
    path = tmp_path / "memory.db"
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")

    with UnitOfWorkAdapter(
        str(path), authoritative_fact_source=source
    ) as unit_of_work:
        assert unit_of_work.memory.add_fact(
            "fact-1", "repo-a", "live source write"
        ) is None
        assert unit_of_work.memory.delete_fact("fact-1", "repo-a") is True

    journal = SQLiteMemoryReplicationJournal(
        path,
        source_id="node-a",
        project_scope="repo-a",
    )
    try:
        assert [(record.sequence, record.version, record.operation) for record in journal.export(limit=10).records] == [
            (1, 1, "upsert"),
            (2, 2, "delete"),
        ]
    finally:
        journal.close()


def test_untouched_authoritative_fact_uow_does_not_reserve_writer_lock(tmp_path):
    """Opening an opt-in UoW must not block a separate SQLite writer."""
    path = tmp_path / "memory.db"
    peer = connect(path)
    peer.execute("PRAGMA busy_timeout=0")
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")

    try:
        with UnitOfWorkAdapter(
            str(path), authoritative_fact_source=source
        ) as unit_of_work:
            peer.execute(
                "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
                ("peer-fact", "repo-a", "peer writer remains available", None),
            )
            peer.commit()

            assert unit_of_work.memory.facts_for_project("repo-a") == [
                {
                    "id": "peer-fact",
                    "project": "repo-a",
                    "text": "peer writer remains available",
                    "embedding": None,
                }
            ]
    finally:
        peer.close()


def test_authoritative_fact_uow_rolls_back_source_state_after_later_failure(tmp_path):
    """An injected source must share the UoW's rollback boundary."""
    path = tmp_path / "memory.db"
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")

    with pytest.raises(RuntimeError, match="abort unit of work"):
        with UnitOfWorkAdapter(
            str(path), authoritative_fact_source=source
        ) as unit_of_work:
            unit_of_work.memory.add_fact(
                "fact-1", "repo-a", "must roll back with the unit of work"
            )
            raise RuntimeError("abort unit of work")

    connection = connect(path)
    try:
        assert facts_for_project(connection, "repo-a") == []
        for table in (
            "memory_authoritative_fact_state",
            "memory_replication_log",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 0
        assert tuple(connection.execute(
            "SELECT source_id,project_scope FROM memory_authoritative_fact_activation"
        ).fetchone()) == ("node-a", "repo-a")
    finally:
        connection.close()


def test_default_unit_of_work_preserves_the_legacy_unjournaled_fact_path(tmp_path):
    path = tmp_path / "memory.db"

    with UnitOfWorkAdapter(str(path)) as unit_of_work:
        unit_of_work.memory.add_fact("fact-1", "repo-a", "legacy fact")

    connection = connect(path)
    try:
        assert facts_for_project(connection, "repo-a")[0]["text"] == "legacy fact"
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_replication_meta"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_replication_log"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_authoritative_fact_source_persists_an_explicit_epoch_advance(tmp_path):
    path = tmp_path / "memory.db"
    connection = connect(path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")

    source.advance_epoch(connection, 2)
    record = source.add_fact(connection, "fact-1", "repo-a", "new epoch fact")

    assert (record.source_epoch, record.sequence, record.version) == (2, 1, 1)
    connection.close()

    journal = SQLiteMemoryReplicationJournal(
        path,
        source_id="node-a",
        project_scope="repo-a",
    )
    try:
        assert journal.export().source_epoch == 2
    finally:
        journal.close()


def test_authoritative_fact_source_rejects_epoch_rollover_after_a_mutation(tmp_path):
    connection = connect(tmp_path / "memory.db")
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(connection, "fact-1", "repo-a", "existing fact")

    with pytest.raises(MemoryReplicationError, match="empty"):
        source.advance_epoch(connection, 2)

    assert connection.execute(
        "SELECT source_epoch FROM memory_replication_meta WHERE source_id=?",
        ("node-a",),
    ).fetchone()[0] == 1
    connection.close()


def test_authoritative_fact_source_rejects_epoch_rollover_after_full_prune(tmp_path):
    path = tmp_path / "memory.db"
    connection = connect(path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(connection, "fact-1", "repo-a", "first value")
    assert source.delete_fact(connection, "fact-1", "repo-a") is True
    connection.close()

    journal = SQLiteMemoryReplicationJournal(
        path,
        source_id="node-a",
        project_scope="repo-a",
    )
    try:
        assert journal.prune_before(3, retain_tombstones=False) == 2
        assert journal.export().records == ()
    finally:
        journal.close()

    reopened = connect(path)
    try:
        with pytest.raises(MemoryReplicationError, match="bootstrap"):
            source.advance_epoch(reopened, 2)
        assert tuple(
            reopened.execute(
                "SELECT source_epoch,next_sequence FROM memory_replication_meta "
                "WHERE source_id=?",
                ("node-a",),
            ).fetchone()
        ) == (1, 3)
    finally:
        reopened.close()


def test_authoritative_fact_source_refuses_a_second_source_for_the_same_fact(tmp_path):
    connection = connect(tmp_path / "memory.db")
    first = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    second = SQLiteAuthoritativeFactSource("node-b", project_scope="repo-a")
    first.add_fact(connection, "fact-1", "repo-a", "node a owns this")

    with pytest.raises(MemoryReplicationError, match="another source"):
        second.upsert_fact(connection, "fact-1", "repo-a", "node b cannot replace it")

    assert facts_for_project(connection, "repo-a")[0]["text"] == "node a owns this"
    owner = connection.execute(
        "SELECT source_id,version,tombstoned "
        "FROM memory_authoritative_fact_state WHERE project=? AND fact_id=?",
        ("repo-a", "fact-1"),
    ).fetchone()
    assert tuple(owner) == ("node-a", 1, 0)
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_replication_log WHERE source_id=?",
        ("node-b",),
    ).fetchone()[0] == 0
    connection.close()


def test_authoritative_fact_source_never_widens_its_project_scope(tmp_path):
    connection = connect(tmp_path / "memory.db")
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")

    with pytest.raises(MemoryReplicationError, match="scope"):
        source.add_fact(connection, "fact-1", "repo-b", "wrong project")

    assert facts_for_project(connection, "repo-a") == []
    assert facts_for_project(connection, "repo-b") == []
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_replication_meta"
    ).fetchone()[0] == 0
    connection.close()


def test_authoritative_fact_state_keeps_versions_after_explicit_journal_pruning(tmp_path):
    path = tmp_path / "memory.db"
    connection = connect(path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(connection, "fact-1", "repo-a", "first")
    assert source.delete_fact(connection, "fact-1", "repo-a") is True
    connection.close()

    journal = SQLiteMemoryReplicationJournal(
        path,
        source_id="node-a",
        project_scope="repo-a",
    )
    try:
        assert journal.prune_before(3, retain_tombstones=False) >= 1
    finally:
        journal.close()

    reopened = connect(path)
    restored = source.upsert_fact(reopened, "fact-1", "repo-a", "restored")
    assert (restored.sequence, restored.version) == (3, 3)
    reopened.close()
