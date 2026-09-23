"""Live-composition fences for authoritative fact writes (issue #514).

These tests exercise the real memory composition root rather than only the
standalone adapter: the live unit-of-work factory activates the configured
source/scope, and every alternate fact writer must then fail closed without a
visible half-write.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys

import pytest

from sonder_runtime.adapters.memory_store import connect, facts_for_project
from sonder_runtime.adapters.persistence.sqlite.authoritative_memory import (
    SQLiteAuthoritativeFactSource,
)
from sonder_runtime.adapters.persistence.sqlite.memory_projection import (
    SQLiteMemoryReplicationProjection,
)
from sonder_runtime.adapters.persistence.sqlite.memory_replication import (
    SQLiteFactReplicationSink,
    SQLiteMemoryReplicationJournal,
)
from sonder_runtime.bootstrap.app import build_application, compose_memory_unit_of_work
from sonder_runtime.domain.memory.replication import MemoryReplicationError
from sonder_runtime.platform.config import Secrets, SonderConfig
from sonder_runtime.platform.memory_replication_config import (
    MemoryReplicationConfig, MemoryReplicationPeerConfig,
)


def _config() -> SonderConfig:
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


def _peer_batch(tmp_path, *, project="repo-a", fact_id="peer-fact"):
    """Export a real node-b authoritative journal page for ``project``."""
    peer_path = tmp_path / f"peer-{project}.db"
    peer = connect(peer_path)
    try:
        SQLiteAuthoritativeFactSource("node-b", project_scope=project).add_fact(
            peer, fact_id, project, "peer asserted fact",
        )
    finally:
        peer.close()
    journal = SQLiteMemoryReplicationJournal(
        peer_path, source_id="node-b", project_scope=project,
    )
    try:
        return journal.export(limit=10)
    finally:
        journal.close()


def _activate_live_scope(path) -> None:
    factory = compose_memory_unit_of_work(_config())
    with factory(str(path)) as scope:
        scope.memory.add_fact("local-fact", "repo-a", "locally journaled fact")


def _snapshot(connection) -> list[str]:
    return list(connection.iterdump())


def test_peer_projection_cannot_bypass_an_active_live_scope(tmp_path):
    path = tmp_path / "memory.db"
    _activate_live_scope(path)
    batch = _peer_batch(tmp_path)

    connection = connect(path)
    try:
        projection = SQLiteMemoryReplicationProjection(connection)
        before = _snapshot(connection)
        with pytest.raises(MemoryReplicationError, match="authoritative project scope"):
            projection.apply(batch)
        assert connection.in_transaction is False
        assert _snapshot(connection) == before
        # Rebuild re-materializes from the projection state; it must honor the
        # same fence and cannot be used to smuggle rows in later.
        with pytest.raises(MemoryReplicationError, match="authoritative project scope"):
            connection.execute(
                "INSERT INTO memory_projection_state"
                "(source_id,source_epoch,sequence,entity_kind,entity_id,version,"
                "operation,project,payload_json,recorded_at,digest) "
                "VALUES('node-b',1,1,'fact','peer-fact',1,'upsert','repo-a',?,"
                "'2026-09-23T00:00:00+00:00','0')",
                (json.dumps({"text": "smuggled", "embedding": None}),),
            )
            connection.commit()
            projection.rebuild(source_id="node-b", project="repo-a")
        assert [row["id"] for row in facts_for_project(connection, "repo-a")] == [
            "local-fact",
        ]
    finally:
        connection.close()

    # The live authority still verifies and accepts the next write after restart.
    factory = compose_memory_unit_of_work(_config())
    with factory(str(path)) as scope:
        scope.memory.add_fact("after-reject", "repo-a", "still authoritative")
        assert [row["id"] for row in scope.memory.facts_for_project("repo-a")] == [
            "local-fact", "after-reject",
        ]


def test_live_receiver_sink_rejects_peer_facts_without_half_write(tmp_path):
    path = tmp_path / "memory.db"
    _activate_live_scope(path)
    batch = _peer_batch(tmp_path)

    connection = connect(path)
    try:
        sink = SQLiteFactReplicationSink("node-a", connection, project_scope="repo-a")
        before = _snapshot(connection)
        with pytest.raises(MemoryReplicationError, match="authoritative project scope"):
            sink.apply(batch)
        assert connection.in_transaction is False
        # Receiver journal, projection log/cursor, fact rows, and indexes all
        # rolled back together: no receipt evidence and no visible fact.
        assert _snapshot(connection) == before
    finally:
        connection.close()


def test_projection_into_an_unactivated_scope_keeps_legacy_behavior(tmp_path):
    path = tmp_path / "memory.db"
    _activate_live_scope(path)
    batch = _peer_batch(tmp_path, project="repo-b")

    connection = connect(path)
    try:
        assert SQLiteMemoryReplicationProjection(connection).apply(batch) == 1
        assert [row["id"] for row in facts_for_project(connection, "repo-b")] == [
            "peer-fact",
        ]
    finally:
        connection.close()


def test_older_schema_copy_migrates_then_activates_through_live_root(
    tmp_path, monkeypatch, capsys,
):
    """Backed-up older-schema copy -> schema upgrade -> adoption -> live writes."""
    from scripts import migrate_authoritative_facts as command

    original = tmp_path / "original.db"
    legacy = sqlite3.connect(original)
    # The facts DDL exactly as first introduced (6f3b1221), with no
    # authoritative state/activation/journal tables and an unstamped schema.
    legacy.execute(
        "CREATE TABLE facts (id TEXT PRIMARY KEY, project TEXT, text TEXT, "
        "embedding BLOB, ts TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    legacy.executemany(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        [
            ("legacy-1", "repo-a", "adopted legacy fact", None),
            ("other", "repo-b", "outside the approved scope", None),
        ],
    )
    legacy.commit()
    legacy.close()
    pristine = original.read_bytes()

    # Operator protocol: work on a copy, keep a schema-upgrade backup, then
    # upgrade with the normal memory store before fact adoption.
    database = tmp_path / "memory.db"
    shutil.copyfile(original, database)
    shutil.copyfile(database, tmp_path / "pre-schema-upgrade.db")
    connect(database).close()

    # Before migration the live root must fail closed over unjournaled facts
    # and must not publish the authority marker.
    monkeypatch.setenv("SONDER_DB", str(database))
    refused = build_application(config=_config())
    try:
        with pytest.raises(MemoryReplicationError, match="migration"):
            with refused.unit_of_work():
                pass
    finally:
        refused.close_providers()
    probe = sqlite3.connect(database)
    try:
        assert probe.execute(
            "SELECT COUNT(*) FROM memory_authoritative_fact_activation"
        ).fetchone()[0] == 0
        assert probe.execute("SELECT COUNT(*) FROM memory_replication_log").fetchone()[0] == 0
    finally:
        probe.close()

    base = [
        "migrate_authoritative_facts.py", "--database", str(database),
        "--source-id", "node-a", "--project", "repo-a",
    ]
    monkeypatch.setattr(sys, "argv", base)
    assert command.main() == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["count"] == 1
    backup = tmp_path / "pre-adoption.db"
    monkeypatch.setattr(
        sys, "argv", base + ["--apply", "--digest", planned["digest"], "--backup", str(backup)],
    )
    assert command.main() == 0
    assert json.loads(capsys.readouterr().out)["migrated"] == 1

    application = build_application(config=_config())
    try:
        with application.unit_of_work() as scope:
            assert [row["id"] for row in scope.memory.facts_for_project("repo-a")] == [
                "legacy-1",
            ]
            scope.memory.add_fact("live-1", "repo-a", "first live write")
    finally:
        application.close_providers()

    restarted = build_application(config=_config())
    try:
        with restarted.unit_of_work() as scope:
            assert [row["id"] for row in scope.memory.facts_for_project("repo-a")] == [
                "legacy-1", "live-1",
            ]
            assert scope.memory.delete_fact("legacy-1", "repo-a") is True
    finally:
        restarted.close_providers()

    journal = SQLiteMemoryReplicationJournal(
        database, source_id="node-a", project_scope="repo-a",
    )
    try:
        assert [
            (record.sequence, record.entity_id, record.version, record.operation)
            for record in journal.export(limit=10).records
        ] == [
            (1, "legacy-1", 1, "upsert"),
            (2, "live-1", 1, "upsert"),
            (3, "legacy-1", 2, "delete"),
        ]
    finally:
        journal.close()
    readback = sqlite3.connect(database)
    try:
        # The unapproved project is never adopted or rewritten.
        assert readback.execute(
            "SELECT text FROM facts WHERE id='other' AND project='repo-b'"
        ).fetchone() == ("outside the approved scope",)
    finally:
        readback.close()
    assert original.read_bytes() == pristine


# --- receiver startup refuses an authoritatively owned scope (review P2) ---


def test_live_graph_receiver_fails_fast_with_operator_error(tmp_path, monkeypatch):
    from sonder_runtime.platform.config import ConfigError
    from tests.test_memory_replication_service import _config as receiver_config

    monkeypatch.setenv("SONDER_DB", str(tmp_path / "memory.db"))
    application = build_application(config=receiver_config(tmp_path))
    try:
        service = application.memory_replication
        service.start()
        with pytest.raises(ConfigError, match="receiver_enabled cannot receive peer facts"):
            service.receiver()
        # The refusal happens before a receiver connection is opened or kept.
        assert service._receiver is None and service._receiver_sink is None
    finally:
        application.close_providers()


def test_standalone_receiver_refuses_a_database_with_an_active_scope(tmp_path):
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )
    from sonder_runtime.platform.config import ConfigError
    from tests.test_memory_replication_service import _config as receiver_config

    owned = tmp_path / "owned.db"
    _activate_live_scope(owned)
    service = compose_memory_replication_service(
        receiver_config(tmp_path), database_path=owned,
    )
    service.start()
    try:
        with pytest.raises(ConfigError, match="owned by the local authoritative fact source"):
            service.receiver()
    finally:
        service.close()

    # A receiver database without that activation is still served.
    free = compose_memory_replication_service(
        receiver_config(tmp_path / "free"), database_path=tmp_path / "free.db",
    )
    free.start()
    try:
        assert free.receiver() is not None
    finally:
        free.close()


def test_serve_main_refuses_receiver_before_listener_bind(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.interfaces.http import serve
    from tests.test_memory_replication_service import _config as receiver_config

    bootstrap_app.reset_for_tests()
    lifecycle = SimpleNamespace(
        startup=lambda **_kwargs: None,
        begin_ollama_probe=lambda: None,
        stop_probe=lambda: None,
        coordinator=SimpleNamespace(add_flush_hook=lambda _hook: None, draining=False),
        drain=lambda _reason: True,
    )
    for name in (
        "_SESSION_FACADE", "_CONTROL_PLANE_SERVICE",
        "_MEMORY_REPLICATION_RECEIVER", "_MEMORY_REPLICATION_SERVICE",
        "_ARTIFACT_TRANSFER_BINDING", "_APP_CONTROL_BINDING",
    ):
        monkeypatch.setattr(serve, name, None)
    monkeypatch.setattr(serve.sonder_lifecycle, "get", lambda: lifecycle)

    def listener(*_args, **_kwargs):
        pytest.fail("listener must not bind when the receiver is refused")

    try:
        with pytest.raises(Exception, match="receiver_enabled cannot receive peer facts"):
            serve.main(receiver_config(tmp_path), _server_factory=listener)
        assert serve._MEMORY_REPLICATION_RECEIVER is None
        assert serve._MEMORY_REPLICATION_SERVICE is None
    finally:
        bootstrap_app.reset_for_tests()
