"""Focused evidence for the explicit, fact-only replication service.

These tests use an in-process receiver adapter.  They do not establish a live
two-host listener, TLS deployment, peer discovery, or retry loop.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os

import pytest

from sonder_runtime.adapters.memory_store import connect, facts_for_project
from sonder_runtime.adapters.persistence.sqlite.authoritative_memory import (
    SQLiteAuthoritativeFactSource,
)
from sonder_runtime.adapters.persistence.sqlite.memory_replication import (
    SQLiteFactReplicationSink,
)
from sonder_runtime.application.memory.replication import MemoryReplicationReceiver
from sonder_runtime.domain.common.errors import DependencyUnavailable
from sonder_runtime.domain.memory.replication import (
    MemoryReplicaReceipt,
    MemoryReplicationError,
)
from sonder_runtime.domain.operational_capabilities import (
    build_operational_capabilities,
)
from sonder_runtime.platform.config import (
    ConfigError,
    Secrets,
    ServerConfig,
    SonderConfig,
    StateConfig,
)
from sonder_runtime.platform.memory_replication_config import (
    MemoryReplicationConfig,
    MemoryReplicationPeerConfig,
)


def _key(seed: str = "m") -> str:
    return "memory-replication-" + seed * 48


def _state_key(seed: str = "s") -> str:
    return "memory-replication-state-" + seed * 48


def _config(tmp_path, *, receiver_enabled: bool = True) -> SonderConfig:
    return SonderConfig(
        state=StateConfig(home=str(tmp_path / "state")),
        server=ServerConfig(host="127.0.0.1"),
        secrets=Secrets(
            api_key="api-" + "a" * 32,
            artifact_transfer_key="artifact-" + "b" * 32,
            auth_secret="auth-" + "c" * 32,
            memory_replication_key=_key(),
            memory_replication_state_integrity_key=_state_key(),
        ),
        memory_replication=MemoryReplicationConfig(
            enabled=True,
            local_node_id="node-a",
            project_scope="repo-a",
            receiver_enabled=receiver_enabled,
            accepted_source_ids=("node-b",) if receiver_enabled else (),
            peers=(
                MemoryReplicationPeerConfig(
                    node_id="node-b",
                    project_scope="repo-a",
                    origin="https://node-b.example:8443",
                ),
            ),
            request_timeout_seconds=3,
            max_batch_records=8,
        ),
    )


def _write_authoritative_fact(
    path,
    *,
    fact_id: str = "fact-1",
    text: str = "authoritative source",
):
    connection = connect(path)
    try:
        SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a").add_fact(
            connection, fact_id, "repo-a", text,
        )
    finally:
        connection.close()


def _durable_receipt(peer_id, batch):
    return MemoryReplicaReceipt(
        replica_id=peer_id,
        source_id=batch.source_id,
        source_epoch=batch.source_epoch,
        next_sequence=batch.next_sequence,
        batch_digest=batch.digest,
        durable=True,
        inserted_records=len(batch.records),
    )


def test_disabled_config_constructs_no_service_or_peer_client(tmp_path):
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )

    constructed = []

    def sink_factory(**_kwargs):
        constructed.append(True)
        raise AssertionError("disabled replication must not construct a peer client")

    service = compose_memory_replication_service(
        SonderConfig(), database_path=tmp_path / "memory.db", sink_factory=sink_factory,
    )

    assert service is None
    assert constructed == []
    capabilities = build_operational_capabilities(
        config=SonderConfig(), memory_replication_status=None,
    )
    assert capabilities["mobility"]["memory_replication_transport"]["available"] is False


def test_explicit_fact_replication_projects_target_before_durable_receipt(tmp_path):
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )

    source_path = tmp_path / "source.db"
    target_path = tmp_path / "target.db"
    _write_authoritative_fact(source_path)
    target = connect(target_path)
    receiver = MemoryReplicationReceiver(
        SQLiteFactReplicationSink("node-b", target, project_scope="repo-a"),
        api_key=_key(),
        accepted_source_ids=("node-a",),
    )
    sent = []

    class InProcessPeer:
        identity = "node-b"

        def apply(self, batch):
            sent.append(batch)
            receipt = receiver.receive(
                "Bearer " + _key(),
                {"object": "memory_replication_batch", "batch": batch.as_dict()},
            )
            # The target normal fact is the proof the receipt follows projection.
            assert facts_for_project(target, "repo-a") == [
                {
                    "id": "fact-1",
                    "project": "repo-a",
                    "text": "authoritative source",
                    "embedding": None,
                }
            ]
            return receipt

    try:
        service = compose_memory_replication_service(
            _config(tmp_path), database_path=source_path,
            sink_factory=lambda **_kwargs: InProcessPeer(),
        )
        assert service is not None
        assert service.status()["started"] is False
        service.start()

        outcome = service.replicate_once()

        assert outcome.status == "replicated"
        assert outcome.replica_ids == ("node-a", "node-b")
        assert len(sent) == 1
        status = service.status()
        assert status["last_attempt"]["durable_receipt_peer_ids"] == ("node-b",)
        assert status["last_attempt"]["next_sequence"] == 1
        status["last_attempt"]["durable_receipts"][0]["peer_id"] = "tampered"
        assert service.status()["last_attempt"]["durable_receipts"][0]["peer_id"] == "node-b"
        assert status["automatic_takeover_available"] is False
        assert status["automatic_failback_available"] is False
    finally:
        target.close()


def test_stopped_configured_peer_is_pending_without_spontaneous_retry(tmp_path):
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )

    source_path = tmp_path / "source.db"
    _write_authoritative_fact(source_path)
    calls = []

    class StoppedPeer:
        identity = "node-b"

        def apply(self, _batch):
            calls.append("attempt")
            raise DependencyUnavailable("peer stopped")

    service = compose_memory_replication_service(
        _config(tmp_path), database_path=source_path,
        sink_factory=lambda **_kwargs: StoppedPeer(),
    )
    assert service is not None
    service.start()

    outcome = service.replicate_once()

    assert outcome.status == "pending"
    assert outcome.failed_replica_ids == ("node-b",)
    assert outcome.failure_reasons == (("node-b", "sink_failure"),)
    assert calls == ["attempt"]
    assert service.status()["last_attempt"]["failure_reasons"] == (
        ("node-b", "sink_failure"),
    )
    service.status()
    service.close()
    assert calls == ["attempt"]


def test_malformed_peer_factory_cannot_replace_the_fixed_peer_identity(tmp_path):
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )

    source_path = tmp_path / "source.db"
    _write_authoritative_fact(source_path)

    class HostilePeer:
        @property
        def identity(self):
            raise AssertionError("identity must not select a peer")

    service = compose_memory_replication_service(
        _config(tmp_path), database_path=source_path,
        sink_factory=lambda **_kwargs: HostilePeer(),
    )
    assert service is not None
    service.start()

    outcome = service.replicate_once()

    assert outcome.status == "pending"
    assert outcome.failed_replica_ids == ("node-b",)
    assert outcome.failure_reasons == (("node-b", "sink_failure"),)


def test_hostile_peer_identity_subclass_cannot_select_a_configured_peer(tmp_path):
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )

    source_path = tmp_path / "source.db"
    _write_authoritative_fact(source_path)

    class SpoofedIdentity(str):
        def __eq__(self, _other):
            return True

        def __ne__(self, _other):
            return False

    class HostilePeer:
        identity = SpoofedIdentity("wrong-peer")

    service = compose_memory_replication_service(
        _config(tmp_path), database_path=source_path,
        sink_factory=lambda **_kwargs: HostilePeer(),
    )
    assert service is not None
    service.start()

    outcome = service.replicate_once()

    assert outcome.status == "pending"
    assert outcome.failed_replica_ids == ("node-b",)
    assert outcome.failure_reasons == (("node-b", "sink_failure"),)


def test_service_is_local_until_explicit_start_and_replication(tmp_path):
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )

    constructed = []
    service = compose_memory_replication_service(
        _config(tmp_path), database_path=tmp_path / "source.db",
        sink_factory=lambda **_kwargs: constructed.append(True),
    )
    assert service is not None

    before = service.status()
    assert before["started"] is False
    assert before["journal"]["opened"] is False
    assert constructed == []
    service.start()
    after_start = service.status()
    assert after_start["started"] is True
    assert after_start["last_attempt"] is None
    assert after_start["persistence"] == {
        "state": "empty", "generation": 0, "restart_safe": True,
    }
    assert not (tmp_path / "state" / "memory-replication-state.json").exists()
    assert constructed == []
    service.close()
    assert service.status()["closed"] is True
    assert constructed == []
    with pytest.raises(RuntimeError, match="closed"):
        service.replicate_once()


def test_construction_and_status_do_not_resolve_the_legacy_database_path(monkeypatch, tmp_path):
    from sonder_runtime.bootstrap.memory_replication import MemoryReplicationService
    from sonder_runtime.platform import paths as runtime_paths

    config = replace(_config(tmp_path), state=StateConfig())
    monkeypatch.setattr(
        runtime_paths,
        "memory_db_path",
        lambda: (_ for _ in ()).throw(AssertionError("status must not resolve storage")),
    )

    service = MemoryReplicationService(config)

    assert service.status()["journal"]["opened"] is False
    service.start()
    assert service.status()["last_attempt"] is None
    service.close()


def test_normal_application_roots_own_the_same_lazy_service(tmp_path):
    from sonder_runtime.bootstrap import app as bootstrap_app

    bootstrap_app.reset_for_tests()
    application = bootstrap_app.default_app(config=_config(tmp_path))
    try:
        service = application.memory_replication
        assert service is not None
        assert service.status()["started"] is False
        assert not (tmp_path / "state" / "memory.db").exists()
        assert bootstrap_app.default_app().memory_replication is service
    finally:
        bootstrap_app.reset_for_tests()


def test_http_owner_composes_only_the_local_receiver_without_peer_send(tmp_path, monkeypatch):
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )
    from sonder_runtime.interfaces.http import serve

    peer_clients = []
    service = compose_memory_replication_service(
        _config(tmp_path), database_path=tmp_path / "target.db",
        sink_factory=lambda **_kwargs: peer_clients.append(True),
    )
    assert service is not None
    monkeypatch.setattr(serve, "_MEMORY_REPLICATION_RECEIVER", None)
    monkeypatch.setattr(serve, "_MEMORY_REPLICATION_SERVICE", None)

    serve.configure_memory_replication_service(service)

    assert serve._MEMORY_REPLICATION_SERVICE is service
    assert serve._MEMORY_REPLICATION_RECEIVER is service.receiver()
    assert service.status()["started"] is True
    assert peer_clients == []
    serve.configure_memory_replication_service(None)
    assert serve._MEMORY_REPLICATION_RECEIVER is None
    assert service.status()["closed"] is True


def test_disabled_typed_http_config_leaves_no_memory_receiver_route(monkeypatch):
    from sonder_runtime.interfaces.http import serve

    closed = []
    stale = type("StaleService", (), {"close": lambda self: closed.append(True)})()
    monkeypatch.setattr(serve, "_MEMORY_REPLICATION_RECEIVER", object())
    monkeypatch.setattr(serve, "_MEMORY_REPLICATION_SERVICE", stale)
    monkeypatch.setattr(serve, "_ARTIFACT_TRANSFER_BINDING", None)
    monkeypatch.setattr(serve, "_APP_CONTROL_BINDING", None)

    serve.configure_typed_config(SonderConfig())

    assert serve._MEMORY_REPLICATION_RECEIVER is None
    assert serve._MEMORY_REPLICATION_SERVICE is None
    assert closed == [True]


def test_serve_main_uses_the_owned_service_before_listener_bind(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.interfaces.http import serve

    bootstrap_app.reset_for_tests()
    observed = {}

    class FakeServer:
        def __init__(self, _address, _handler):
            observed["service"] = serve._MEMORY_REPLICATION_SERVICE
            observed["receiver"] = serve._MEMORY_REPLICATION_RECEIVER

        def serve_forever(self):
            return None

        def server_close(self):
            return None

        def shutdown(self):
            return None

    lifecycle = SimpleNamespace(
        startup=lambda **_kwargs: None,
        begin_ollama_probe=lambda: None,
        coordinator=SimpleNamespace(
            add_flush_hook=lambda _hook: None,
            draining=False,
        ),
        drain=lambda _reason: None,
    )
    monkeypatch.setattr(serve, "_SESSION_FACADE", None)
    monkeypatch.setattr(serve, "_CONTROL_PLANE_SERVICE", None)
    monkeypatch.setattr(serve, "_MEMORY_REPLICATION_RECEIVER", None)
    monkeypatch.setattr(serve, "_MEMORY_REPLICATION_SERVICE", None)
    monkeypatch.setattr(serve, "_ARTIFACT_TRANSFER_BINDING", None)
    monkeypatch.setattr(serve, "_APP_CONTROL_BINDING", None)
    monkeypatch.setattr(serve.sonder_lifecycle, "get", lambda: lifecycle)
    monkeypatch.setattr(serve.server, "runtime_source_update_status", lambda refresh=False: "ok")
    try:
        serve.main(_config(tmp_path), _server_factory=FakeServer)
        assert observed["service"] is not None
        assert observed["service"].status()["started"] is True
        assert observed["receiver"] is not None
        assert observed["service"].status()["closed"] is True
    finally:
        bootstrap_app.reset_for_tests()


def test_receiver_admission_uses_only_fixed_scope_identity_and_secret(tmp_path):
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )
    from sonder_runtime.domain.memory.replication import MemoryMutation, MemoryReplicationBatch

    service = compose_memory_replication_service(
        _config(tmp_path), database_path=tmp_path / "target.db",
    )
    assert service is not None
    service.start()
    receiver = service.receiver()
    assert receiver is not None

    def mutation(*, source_id="node-b", project="repo-a"):
        return MemoryMutation(
            source_id=source_id, source_epoch=1, sequence=1,
            entity_kind="fact", entity_id="fact-1", version=1,
            operation="upsert", project=project, payload={"text": "fact"},
            recorded_at="2026-09-07T00:00:00+00:00",
        )

    accepted_mutation = mutation()
    accepted = MemoryReplicationBatch("node-b", 1, 0, (accepted_mutation,), 1, False)
    with pytest.raises(PermissionError, match="authentication"):
        receiver.receive("Bearer wrong", {"object": "memory_replication_batch", "batch": accepted.as_dict()})
    wrong_source = mutation(source_id="node-c")
    with pytest.raises(PermissionError, match="source"):
        receiver.receive(
            "Bearer " + _key(),
            {"object": "memory_replication_batch", "batch": MemoryReplicationBatch("node-c", 1, 0, (wrong_source,), 1, False).as_dict()},
        )
    wrong_scope = mutation(project="repo-b")
    with pytest.raises(MemoryReplicationError, match="project"):
        receiver.receive(
            "Bearer " + _key(),
            {"object": "memory_replication_batch", "batch": MemoryReplicationBatch("node-b", 1, 0, (wrong_scope,), 1, False).as_dict()},
        )
    assert facts_for_project(receiver.sink.connection, "repo-a") == []
    service.close()


def test_enabled_service_capability_is_fact_only_and_denies_takeover(tmp_path):
    from sonder_runtime.bootstrap.memory_replication import (
        MemoryReplicationService,
    )

    config = _config(tmp_path)
    service = MemoryReplicationService(config, database_path=tmp_path / "source.db")
    status = service.status()
    capabilities = build_operational_capabilities(
        config=config, memory_replication_status=status,
    )

    transport = capabilities["mobility"]["memory_replication_transport"]
    assert transport["available"] is True
    assert transport["automatic_takeover_available"] is False
    assert transport["automatic_failback_available"] is False
    assert capabilities["mobility"]["automatic_memory_migration"]["available"] is False
    assert "fact" in transport["reason"].lower()
    invalid = replace(
        config,
        memory_replication=replace(config.memory_replication, project_scope="repo-other"),
    )
    with pytest.raises(ConfigError):
        MemoryReplicationService(invalid, database_path=tmp_path / "other.db")


def test_restart_restores_persisted_cursor_and_durable_receipt(tmp_path, monkeypatch):
    from sonder_runtime.bootstrap import memory_replication as module
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )

    source_path = tmp_path / "source.db"
    _write_authoritative_fact(source_path)
    fsyncs = []
    original_fsync = module.os.fsync
    monkeypatch.setattr(
        module.os,
        "fsync",
        lambda descriptor: fsyncs.append(descriptor) or original_fsync(descriptor),
    )
    sent = []

    class Peer:
        identity = "node-b"

        def apply(self, batch):
            sent.append(batch)
            return _durable_receipt(self.identity, batch)

    first = compose_memory_replication_service(
        _config(tmp_path), database_path=source_path,
        sink_factory=lambda **_kwargs: Peer(),
    )
    assert first is not None
    first.start()
    assert first.replicate_once().status == "replicated"
    state_path = tmp_path / "state" / "memory-replication-state.json"
    anchor_path = tmp_path / "state" / "memory-replication-anchor.json"
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert stored["cursor"] == 1
    assert stored["last_attempt"]["durable_receipts"] == [
        {"next_sequence": 1, "peer_id": "node-b", "source_epoch": 1},
    ]
    assert _key() not in state_path.read_text(encoding="utf-8")
    assert _state_key() not in state_path.read_text(encoding="utf-8")
    assert _key() not in anchor_path.read_text(encoding="utf-8")
    assert _state_key() not in anchor_path.read_text(encoding="utf-8")
    assert "node-b.example" not in state_path.read_text(encoding="utf-8")
    assert "node-b.example" not in anchor_path.read_text(encoding="utf-8")
    assert fsyncs
    if os.name == "posix":
        assert state_path.stat().st_mode & 0o077 == 0
    first.close()

    _write_authoritative_fact(
        source_path, fact_id="fact-2", text="later authoritative source",
    )
    second = compose_memory_replication_service(
        _config(tmp_path), database_path=source_path,
        sink_factory=lambda **_kwargs: Peer(),
    )
    assert second is not None
    second.start()
    restored = second.status()
    assert restored["persistence"]["state"] == "restored"
    assert restored["journal"]["cursor"] == 1
    assert restored["last_attempt"]["durable_receipt_peer_ids"] == ("node-b",)
    assert second.replicate_once().status == "replicated"
    assert sent[-1].after_sequence == 1


def test_restart_restores_partial_attempt_for_exact_operator_retry(tmp_path):
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )

    source_path = tmp_path / "source.db"
    _write_authoritative_fact(source_path)
    base = _config(tmp_path, receiver_enabled=False)
    section = replace(
        base.memory_replication,
        peers=(
            MemoryReplicationPeerConfig(
                node_id="node-b", project_scope="repo-a",
                origin="https://node-b.example:8443",
            ),
            MemoryReplicationPeerConfig(
                node_id="node-c", project_scope="repo-a",
                origin="https://node-c.example:8443",
            ),
        ),
    )
    config = replace(base, memory_replication=section)

    class FirstAttemptPeer:
        def __init__(self, peer_id):
            self.identity = peer_id

        def apply(self, batch):
            if self.identity == "node-c":
                raise DependencyUnavailable("configured peer stopped")
            return _durable_receipt(self.identity, batch)

    first = compose_memory_replication_service(
        config,
        database_path=source_path,
        sink_factory=lambda *, peer, **_kwargs: FirstAttemptPeer(peer.node_id),
    )
    assert first is not None
    first.start()
    pending = first.replicate_once()
    assert pending.status == "pending"
    assert pending.replica_ids == ("node-a", "node-b")
    first.close()

    retries = []

    class RetryPeer:
        def __init__(self, peer_id):
            self.identity = peer_id

        def apply(self, batch):
            retries.append((self.identity, batch.after_sequence))
            return _durable_receipt(self.identity, batch)

    second = compose_memory_replication_service(
        config,
        database_path=source_path,
        sink_factory=lambda *, peer, **_kwargs: RetryPeer(peer.node_id),
    )
    assert second is not None
    second.start()
    restored = second.status()
    assert restored["persistence"]["state"] == "restored"
    assert restored["journal"]["cursor"] == 0
    assert restored["last_attempt"]["durable_receipt_peer_ids"] == ("node-b",)
    assert restored["last_attempt"]["failed_peer_ids"] == ("node-c",)
    assert second.replicate_once().status == "replicated"
    assert retries == [("node-b", 0), ("node-c", 0)]


def test_restart_restores_failed_attempt_and_refuses_corrupt_or_incompatible_state(
    tmp_path,
):
    from sonder_runtime.bootstrap import memory_replication as module
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )

    source_path = tmp_path / "source.db"
    _write_authoritative_fact(source_path)
    config = _config(tmp_path, receiver_enabled=False)
    first = compose_memory_replication_service(
        config,
        database_path=source_path,
        journal_factory=lambda **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    assert first is not None
    first.start()
    with pytest.raises(DependencyUnavailable, match="source"):
        first.replicate_once()
    first.close()

    resumed_calls = []

    class Peer:
        identity = "node-b"

        def apply(self, batch):
            resumed_calls.append(batch.after_sequence)
            return _durable_receipt(self.identity, batch)

    resumed = compose_memory_replication_service(
        config, database_path=source_path,
        sink_factory=lambda **_kwargs: Peer(),
    )
    assert resumed is not None
    resumed.start()
    assert resumed.status()["last_attempt"]["status"] == "failed"
    assert resumed.replicate_once().status == "replicated"
    assert resumed_calls == [0]
    resumed.close()

    incompatible = replace(
        config,
        memory_replication=replace(config.memory_replication, local_node_id="node-c"),
    )
    blocked = compose_memory_replication_service(
        incompatible, database_path=source_path,
        sink_factory=lambda **_kwargs: pytest.fail("invalid state must not contact a peer"),
    )
    assert blocked is not None
    blocked.start()
    assert blocked.status()["persistence"]["state"] == "incompatible"
    with pytest.raises(DependencyUnavailable, match="state"):
        blocked.replicate_once()

    state_path = tmp_path / "state" / "memory-replication-state.json"
    canonical_state = state_path.read_bytes()
    noncanonical = json.loads(state_path.read_text(encoding="utf-8"))
    noncanonical["schema_version"] = float("nan")
    state_path.write_text(json.dumps(noncanonical), encoding="utf-8")
    noncanonical_state = compose_memory_replication_service(
        config, database_path=source_path,
        sink_factory=lambda **_kwargs: pytest.fail("noncanonical state must not contact a peer"),
    )
    assert noncanonical_state is not None
    noncanonical_state.start()
    assert noncanonical_state.status()["persistence"]["state"] == "corrupt"
    with pytest.raises(DependencyUnavailable, match="state"):
        noncanonical_state.replicate_once()

    tampered = json.loads(canonical_state)
    tampered["integrity"] = "0" * 64
    state_path.write_bytes(module._canonical_state_bytes(tampered))
    tampered_state = compose_memory_replication_service(
        config, database_path=source_path,
        sink_factory=lambda **_kwargs: pytest.fail("tampered state must not contact a peer"),
    )
    assert tampered_state is not None
    tampered_state.start()
    assert tampered_state.status()["persistence"]["state"] == "incompatible"
    with pytest.raises(DependencyUnavailable, match="state"):
        tampered_state.replicate_once()

    state_path.write_text("{not-json", encoding="utf-8")
    corrupt = compose_memory_replication_service(
        config, database_path=source_path,
        sink_factory=lambda **_kwargs: pytest.fail("corrupt state must not contact a peer"),
    )
    assert corrupt is not None
    corrupt.start()
    assert corrupt.status()["persistence"]["state"] == "corrupt"
    with pytest.raises(DependencyUnavailable, match="state"):
        corrupt.replicate_once()


def test_state_write_failure_never_returns_a_success_shaped_attempt(
    tmp_path,
    monkeypatch,
):
    from sonder_runtime.bootstrap import memory_replication as module
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )

    source_path = tmp_path / "source.db"
    _write_authoritative_fact(source_path)
    peer_calls = []

    class Peer:
        identity = "node-b"

        def apply(self, batch):
            peer_calls.append(batch.after_sequence)
            return _durable_receipt(self.identity, batch)

    monkeypatch.setattr(
        module,
        "_write_private_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            module._ReplicationStateError("unavailable"),
        ),
    )
    service = compose_memory_replication_service(
        _config(tmp_path), database_path=source_path,
        sink_factory=lambda **_kwargs: Peer(),
    )
    assert service is not None
    service.start()

    with pytest.raises(DependencyUnavailable, match="state"):
        service.replicate_once()

    status = service.status()
    assert status["persistence"]["state"] == "unavailable"
    assert status["last_attempt"]["status"] == "failed"
    assert status["last_attempt"]["durable_receipt_peer_ids"] == ()
    assert peer_calls == [0]
    with pytest.raises(DependencyUnavailable, match="state"):
        service.replicate_once()
    assert peer_calls == [0]


def test_checkpoint_rejects_a_forgery_signed_with_the_peer_bearer_key(
    tmp_path,
):
    from sonder_runtime.bootstrap import memory_replication as module
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )

    source_path = tmp_path / "source.db"
    _write_authoritative_fact(source_path)
    config = _config(tmp_path)

    class Peer:
        identity = "node-b"

        def apply(self, batch):
            return _durable_receipt(self.identity, batch)

    service = compose_memory_replication_service(
        config, database_path=source_path, sink_factory=lambda **_kwargs: Peer(),
    )
    assert service is not None
    service.start()
    assert service.replicate_once().status == "replicated"
    service.close()

    state_path = tmp_path / "state" / "memory-replication-state.json"
    anchor_path = tmp_path / "state" / "memory-replication-anchor.json"
    checkpoint = json.loads(state_path.read_text(encoding="utf-8"))
    anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
    checkpoint_body = {
        key: value for key, value in checkpoint.items() if key != "integrity"
    }
    checkpoint_body["cursor"] = 0
    checkpoint_body["last_attempt"] = {
        "status": "failed",
        "source_epoch": 0,
        "after_sequence": 0,
        "next_sequence": 0,
        "durable_receipts": [],
        "failed_peer_ids": [],
        "failure_reasons": [{"peer_id": "source", "reason": "source_unavailable"}],
        "inserted_records": 0,
    }
    anchor_body = {key: value for key, value in anchor.items() if key != "integrity"}
    anchor_body["acknowledged_cursor"] = 0
    anchor_body["acknowledged_source_epoch"] = 0
    anchor_body["record_digest"] = ""
    anchor_body["checkpoint_digest"] = hashlib.sha256(
        module._canonical_state_bytes(checkpoint_body)
    ).hexdigest()
    peer_key = config.secrets.memory_replication_key
    checkpoint = {
        **checkpoint_body,
        "integrity": module._state_integrity_tag_for_key(
            peer_key, checkpoint_body, domain="checkpoint",
        ),
    }
    anchor = {
        **anchor_body,
        "integrity": module._state_integrity_tag_for_key(
            peer_key, anchor_body, domain="anchor",
        ),
    }
    state_path.write_bytes(module._canonical_state_bytes(checkpoint))
    anchor_path.write_bytes(module._canonical_state_bytes(anchor))

    attempted = []
    blocked = compose_memory_replication_service(
        config,
        database_path=source_path,
        journal_factory=lambda **_kwargs: attempted.append("journal"),
        sink_factory=lambda **_kwargs: attempted.append("peer"),
    )
    assert blocked is not None
    blocked.start()

    assert blocked.status()["persistence"]["state"] == "incompatible"
    with pytest.raises(DependencyUnavailable, match="state"):
        blocked.replicate_once()
    assert attempted == []


@pytest.mark.parametrize("document", ("checkpoint", "anchor"))
@pytest.mark.parametrize("shape", ("whitespace", "reordered", "duplicate", "nonfinite"))
def test_restart_rejects_noncanonical_state_documents_before_opening_a_peer_or_journal(
    tmp_path, document, shape,
):
    from sonder_runtime.bootstrap import memory_replication as module
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )

    source_path = tmp_path / "source.db"
    _write_authoritative_fact(source_path)
    config = _config(tmp_path)

    class Peer:
        identity = "node-b"

        def apply(self, batch):
            return _durable_receipt(self.identity, batch)

    service = compose_memory_replication_service(
        config, database_path=source_path, sink_factory=lambda **_kwargs: Peer(),
    )
    assert service is not None
    service.start()
    assert service.replicate_once().status == "replicated"
    service.close()

    state_path = tmp_path / "state" / "memory-replication-state.json"
    target_path = (
        state_path
        if document == "checkpoint"
        else tmp_path / "state" / "memory-replication-anchor.json"
    )
    raw = target_path.read_bytes()
    if shape == "whitespace":
        malformed = raw + b" "
    elif shape == "reordered":
        decoded = json.loads(raw)
        malformed = json.dumps(
            {key: decoded[key] for key in reversed(tuple(decoded))},
            separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii")
    elif shape == "duplicate":
        needle = (
            b'"cursor":1,'
            if document == "checkpoint"
            else b'"acknowledged_cursor":1,'
        )
        malformed = raw.replace(needle, needle + needle, 1)
    else:
        malformed = raw.replace(b'"generation":1', b'"generation":NaN', 1)
    assert malformed != raw
    target_path.write_bytes(malformed)

    attempted = []
    blocked = compose_memory_replication_service(
        config,
        database_path=source_path,
        journal_factory=lambda **_kwargs: attempted.append("journal"),
        sink_factory=lambda **_kwargs: attempted.append("peer"),
    )
    assert blocked is not None
    blocked.start()

    assert blocked.status()["persistence"]["state"] == "corrupt"
    with pytest.raises(DependencyUnavailable, match="state"):
        blocked.replicate_once()
    assert attempted == []


@pytest.mark.parametrize("stale_file", ("checkpoint", "anchor"))
def test_restart_rejects_a_stale_checkpoint_anchor_pair_before_peer_or_journal_work(
    tmp_path, stale_file,
):
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )

    source_path = tmp_path / "source.db"
    _write_authoritative_fact(source_path)
    config = _config(tmp_path)

    class Peer:
        identity = "node-b"

        def apply(self, batch):
            return _durable_receipt(self.identity, batch)

    service = compose_memory_replication_service(
        config, database_path=source_path, sink_factory=lambda **_kwargs: Peer(),
    )
    assert service is not None
    service.start()
    assert service.replicate_once().status == "replicated"
    state_path = tmp_path / "state" / "memory-replication-state.json"
    anchor_path = tmp_path / "state" / "memory-replication-anchor.json"
    old_checkpoint = state_path.read_bytes()
    old_anchor = anchor_path.read_bytes()
    _write_authoritative_fact(source_path, fact_id="fact-2", text="later source")
    assert service.replicate_once().status == "replicated"
    service.close()

    if stale_file == "checkpoint":
        state_path.write_bytes(old_checkpoint)
    else:
        anchor_path.write_bytes(old_anchor)

    attempted = []
    blocked = compose_memory_replication_service(
        config,
        database_path=source_path,
        journal_factory=lambda **_kwargs: attempted.append("journal"),
        sink_factory=lambda **_kwargs: attempted.append("peer"),
    )
    assert blocked is not None
    blocked.start()

    assert blocked.status()["persistence"]["state"] == "incompatible"
    with pytest.raises(DependencyUnavailable, match="state"):
        blocked.replicate_once()
    assert attempted == []


def test_restart_rejects_a_journal_rollback_before_contacting_a_peer(tmp_path):
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )

    source_path = tmp_path / "source.db"
    _write_authoritative_fact(source_path)
    config = _config(tmp_path)

    class Peer:
        identity = "node-b"

        def apply(self, batch):
            return _durable_receipt(self.identity, batch)

    service = compose_memory_replication_service(
        config, database_path=source_path, sink_factory=lambda **_kwargs: Peer(),
    )
    assert service is not None
    service.start()
    assert service.replicate_once().status == "replicated"
    service.close()

    connection = connect(source_path)
    try:
        connection.execute(
            "DELETE FROM memory_replication_log WHERE source_id=? AND sequence=?",
            ("node-a", 1),
        )
        connection.commit()
    finally:
        connection.close()

    peer_calls = []
    resumed = compose_memory_replication_service(
        config,
        database_path=source_path,
        sink_factory=lambda **_kwargs: peer_calls.append(True),
    )
    assert resumed is not None
    resumed.start()
    with pytest.raises(DependencyUnavailable, match="state"):
        resumed.replicate_once()

    assert resumed.status()["persistence"]["state"] == "journal_regressed"
    assert peer_calls == []


def test_checkpoint_then_anchor_power_loss_never_reports_success_or_skips_records(
    tmp_path, monkeypatch,
):
    from sonder_runtime.bootstrap import memory_replication as module
    from sonder_runtime.bootstrap.memory_replication import (
        compose_memory_replication_service,
    )

    source_path = tmp_path / "source.db"
    _write_authoritative_fact(source_path)
    config = _config(tmp_path)

    class Peer:
        identity = "node-b"

        def apply(self, batch):
            return _durable_receipt(self.identity, batch)

    service = compose_memory_replication_service(
        config, database_path=source_path, sink_factory=lambda **_kwargs: Peer(),
    )
    assert service is not None
    service.start()
    assert service.replicate_once().status == "replicated"
    _write_authoritative_fact(source_path, fact_id="fact-2", text="later source")

    original = module._write_private_state
    fail_anchor = {"enabled": True}

    def write_with_power_loss(path, payload):
        if fail_anchor["enabled"] and path.name == "memory-replication-anchor.json":
            raise module._ReplicationStateError("unavailable")
        return original(path, payload)

    monkeypatch.setattr(module, "_write_private_state", write_with_power_loss)
    with pytest.raises(DependencyUnavailable, match="state"):
        service.replicate_once()
    assert service.status()["persistence"]["state"] == "unavailable"

    state_path = tmp_path / "state" / "memory-replication-state.json"
    anchor_path = tmp_path / "state" / "memory-replication-anchor.json"
    assert json.loads(state_path.read_text(encoding="utf-8"))["cursor"] == 2
    assert json.loads(anchor_path.read_text(encoding="utf-8"))["acknowledged_cursor"] == 1
    service.close()

    attempted = []
    resumed = compose_memory_replication_service(
        config,
        database_path=source_path,
        journal_factory=lambda **_kwargs: attempted.append("journal"),
        sink_factory=lambda **_kwargs: attempted.append("peer"),
    )
    assert resumed is not None
    resumed.start()
    assert resumed.status()["persistence"]["state"] == "incompatible"
    with pytest.raises(DependencyUnavailable, match="state"):
        resumed.replicate_once()
    assert attempted == []


def test_windows_private_state_replacement_requests_write_through(tmp_path, monkeypatch):
    from sonder_runtime.bootstrap import memory_replication as module

    path = tmp_path / "memory-replication-state.json"
    calls = []

    def move_with_write_through(source, destination, flags):
        calls.append((source, destination, flags))
        os.replace(source, destination)

    monkeypatch.setattr(module, "_is_windows", lambda: True)
    monkeypatch.setattr(module, "_windows_move_file_ex", move_with_write_through)

    module._write_private_state(path, {"kind": "test", "value": 1})

    assert path.read_bytes() == module._canonical_state_bytes(
        {"kind": "test", "value": 1}
    )
    assert calls == [
        (
            calls[0][0],
            path,
            module._MOVEFILE_REPLACE_EXISTING | module._MOVEFILE_WRITE_THROUGH,
        )
    ]
