from dataclasses import replace
import hashlib
import io
import json
import time

import pytest

from sonder_runtime.platform.config import SonderConfig, Secrets, StateConfig
from sonder_runtime.platform.artifact_mobility_config import ArtifactMobilityConfig
from sonder_runtime.platform.artifact_mobility_source_config import ArtifactMobilitySourceConfig
from sonder_runtime.bootstrap.artifact_mobility_source import ArtifactMobilitySourceBinding
from sonder_runtime.application.artifacts.mobility import MobilityJournalError, MobilityOperationRequest


def config_for(tmp_path):
    return SonderConfig(
        state=StateConfig(home=str(tmp_path / 'state')),
        artifact_mobility_source=ArtifactMobilitySourceConfig(
            enabled=True, store_dir=str(tmp_path / 'private-source'),
            principal_id='principal-a', project_id='project-a', source_owner_id='owner-a'),
        artifact_mobility=ArtifactMobilityConfig(
            enabled=True, destination_label='node-one',
            destination_origin='https://private-peer.example:9443',
            destination_tls_certificate_sha256='a' * 64,
            expected_recipient_attestation_sha256='b' * 64,
            destination_credential_id='private-generation'),
        secrets=Secrets(artifact_mobility_peer_key='private-peer-key-' + 'c' * 32),
    )


def trusted_source(config):
    publisher, reader = object(), object()
    source = ArtifactMobilitySourceBinding(lambda: config,
        publisher_capability=publisher, reader_capability=reader)
    data = b'pre-admitted source'
    record = source.publisher_for(publisher).publish_sealed(io.BytesIO(data), {
        'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data),
        'media_type': 'application/octet-stream'}, publisher)
    return source, record


def test_binding_reads_and_close_never_construct_peer(tmp_path, monkeypatch):
    from sonder_runtime.bootstrap.artifact_mobility import ArtifactMobilityBinding
    from sonder_runtime.adapters.compute_fabric import artifact_mobility as peer
    monkeypatch.setattr(peer, 'ConfiguredArtifactMobilityPeer', lambda *a, **k: pytest.fail('peer constructed'))
    config = config_for(tmp_path)
    binding = ArtifactMobilityBinding(lambda: config)
    assert not (tmp_path / 'private-source').exists()
    assert binding.list() == ()
    with pytest.raises(MobilityJournalError, match='NOT_FOUND'):
        binding.status('f' * 32)
    binding.close()
    with pytest.raises(MobilityJournalError, match='UNAVAILABLE'):
        binding.list()


@pytest.mark.parametrize("initial_store", ("missing", "directory", "database", "schema", "malformed", "partial", "unknown", "wal"))
@pytest.mark.parametrize("action", ("list", "status"))
def test_receipt_inspection_never_creates_journal_artifacts(tmp_path, initial_store, action):
    import sqlite3
    from pathlib import Path
    from sonder_runtime.bootstrap.artifact_mobility import compose_artifact_mobility
    from sonder_runtime.adapters.persistence.artifact_mobility import SQLiteArtifactMobilityJournal
    from sonder_runtime.application.compute_fabric.artifact_spool import PrivateDirectoryAnchor

    config = config_for(tmp_path)
    root = Path(config.artifact_mobility_source.store_dir) / "outbound-journal"
    database = root / "artifact-mobility.sqlite"
    if initial_store != "missing":
        with PrivateDirectoryAnchor.open_base(root):
            pass
    if initial_store == "database":
        sqlite3.connect(database).close()
    elif initial_store in ("schema", "partial", "wal"):
        SQLiteArtifactMobilityJournal(root).close()
        if initial_store != "schema":
            with sqlite3.connect(database) as connection:
                if initial_store == "partial":
                    connection.execute("DROP TABLE mobility_tombstones")
                else:
                    connection.execute("PRAGMA journal_mode=WAL")
            connection.close()
    elif initial_store == "malformed":
        database.write_bytes(b"not a SQLite database")
    elif initial_store == "unknown":
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.close()
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    directories = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_dir()}
    _binding, status, listing, _available, close = compose_artifact_mobility(lambda: config)
    try:
        result = listing() if action == "list" else status("f" * 32)
        if initial_store in ("database", "malformed", "partial", "unknown", "wal"):
            assert result == {"outcome_code": "UNAVAILABLE"}
        else:
            assert result == ({"operations": []} if action == "list" else {"outcome_code": "NOT_FOUND"})
        assert {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
        assert {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_dir()} == directories
    finally:
        close()


def test_missing_publisher_and_mismatched_confirmation_fail_before_peer(tmp_path, monkeypatch):
    from sonder_runtime.bootstrap.artifact_mobility import ArtifactMobilityBinding
    from sonder_runtime.adapters.compute_fabric import artifact_mobility as peer
    monkeypatch.setattr(peer, 'ConfiguredArtifactMobilityPeer', lambda *a, **k: pytest.fail('peer constructed'))
    config = config_for(tmp_path)
    absent = ArtifactMobilityBinding(lambda: config)
    with pytest.raises(MobilityJournalError, match='SOURCE_UNAVAILABLE'):
        absent.send('a' * 32, confirm_destination='node-one')
    source, record = trusted_source(config)
    binding = ArtifactMobilityBinding(lambda: config, source_binding=source)
    with pytest.raises(MobilityJournalError, match='CONFIRMATION_REQUIRED'):
        binding.send(record['source_artifact_id'], confirm_destination='wrong')
    with pytest.raises(MobilityJournalError, match='INVALID_REQUEST'):
        binding.send('C:/arbitrary-path', confirm_destination='node-one')
    with pytest.raises(TypeError):
        binding.send(record['source_artifact_id'], confirm_destination='node-one', operation_id='a' * 32)
    with pytest.raises(MobilityJournalError, match='NOT_FOUND'):
        binding.resume('https://private-peer.example:9443')
    binding.close()
    absent.close()


def test_close_preserves_live_lease_and_reopen_recovers_only_expired(tmp_path):
    from sonder_runtime.bootstrap.artifact_mobility import ArtifactMobilityBinding
    config = config_for(tmp_path)
    source, record = trusted_source(config)
    binding = ArtifactMobilityBinding(lambda: config, source_binding=source)
    context = binding._context()
    repository = binding._repository_for_dispatch()
    from sonder_runtime.application.artifacts.mobility import ArtifactMobilityJournal
    journal = ArtifactMobilityJournal(repository)
    request = MobilityOperationRequest(
        source_owner_id=context.source_owner_id, source_scope_id=context.source_scope_id,
        source_artifact_id=record['source_artifact_id'],
        immutable_spec={k: record[k] for k in ('sha256', 'size_bytes', 'media_type')},
        destination_label=context.destination_label, destination_scope_id=context.destination_scope_id,
        credential_generation=context.credential_generation,
        destination_binding_hmac=context.destination_binding_hmac)
    operation = journal.create_operation(request, credential_material=context.credential_material)
    lock = repository.try_acquire_dispatch_lock(operation.operation_id)
    lease = repository.acquire_dispatch(operation.operation_id, context.source_owner_id,
        now=time.time(), lease_seconds=90, lock=lock)
    binding.close()
    reopened = ArtifactMobilityBinding(lambda: config)
    assert reopened.status(operation.operation_id)['state'] == 'dispatching'
    assert repository.load_operation(operation.operation_id, context.source_owner_id).lease_token == lease.token
    lock.close()
    repository.recover_expired_leases(now=lease.expires_at + 1)
    assert reopened.status(operation.operation_id)['state'] == 'resumable'
    encoded = json.dumps(reopened.list())
    for private in (config.artifact_mobility.destination_origin, 'a' * 64, 'b' * 64,
                    context.destination_binding_hmac, context.credential_generation,
                    config.secrets.artifact_mobility_peer_key, config.artifact_mobility_source.store_dir):
        assert private not in encoded
    assert set(reopened.status(operation.operation_id)) == {
        'operation_id', 'source_artifact_id', 'destination_label', 'state',
        'outcome_code', 'created_at', 'updated_at'}
    reopened.close()


def test_application_composition_remains_lazy(tmp_path, monkeypatch):
    from sonder_runtime.bootstrap import app
    from sonder_runtime.bootstrap import artifact_mobility
    monkeypatch.setattr(artifact_mobility, 'ArtifactMobilityBinding', lambda *a, **k: pytest.fail('constructed'))
    application = app.build_application(config=config_for(tmp_path))
    assert application.operational_capabilities()['mobility']['automatic_artifact_migration']['available'] is False
    application.close_artifact_mobility()
    assert not (tmp_path / 'private-source').exists()


def test_host_owned_application_send_projects_constrained_peer_failure(tmp_path, monkeypatch):
    from sonder_runtime.bootstrap import app
    from sonder_runtime.adapters.compute_fabric.artifact_mobility import ConfiguredArtifactMobilityPeer
    from sonder_runtime.application.artifacts.transfer import TransferError
    from sonder_runtime.interfaces.repl import repl
    from sonder_runtime import __main__ as cli
    config = config_for(tmp_path)
    source, record = trusted_source(config)
    calls = []

    def refused(self, spec):
        calls.append('attestation')
        raise TransferError('https://private-peer.example:9443 peer-message-sentinel')

    monkeypatch.setattr(ConfiguredArtifactMobilityPeer, 'recipient_attestation', refused)
    application = app.build_application(config=config, _artifact_mobility_source_binding=source)
    capabilities = application.operational_capabilities()['mobility']
    assert capabilities['fixed_peer_artifact_copy']['available'] is True
    assert 'pre-admitted source-only artifact' in capabilities['fixed_peer_artifact_copy']['reason']
    assert 'Operator-invoked' in capabilities['fixed_peer_artifact_copy']['reason']
    assert capabilities['automatic_artifact_migration']['available'] is False
    assert calls == []
    result = application._artifact_mobility_binding().send(record['source_artifact_id'], confirm_destination='node-one')
    assert result['state'] == 'terminal_blocked'
    assert result['outcome_code'] == 'MOBILITY_INTEGRITY'
    assert calls == ['attestation']
    assert application.artifact_mobility_status(result['operation_id']) == result
    assert application.artifact_mobility_list() == {'operations': [result]}
    monkeypatch.setattr(repl.server, '_application', lambda: application)
    assert json.loads(repl._artifact_mobility_command('status ' + result['operation_id'])) == result
    assert json.loads(repl._artifact_mobility_command('list')) == {'operations': [result]}
    encoded = json.dumps([result, application.artifact_mobility_list(), capabilities])
    for secret in ('peer-message-sentinel', config.artifact_mobility.destination_origin,
                   'a' * 64, 'b' * 64, config.secrets.artifact_mobility_peer_key,
                   config.artifact_mobility.destination_credential_id):
        assert secret not in encoded
    application.close_artifact_mobility()
    assert calls == ['attestation']


def test_close_inside_peer_stops_later_dispatch_without_clearing_lease(tmp_path, monkeypatch):
    from sonder_runtime.bootstrap.artifact_mobility import ArtifactMobilityBinding
    from sonder_runtime.adapters.compute_fabric.artifact_mobility import ConfiguredArtifactMobilityPeer
    from sonder_runtime.application.artifacts.transfer import TransferError
    config = config_for(tmp_path)
    source, record = trusted_source(config)
    binding = ArtifactMobilityBinding(lambda: config, source_binding=source)

    def interrupted(self, spec):
        binding.close()
        raise TransferError('MOBILITY_UNAVAILABLE')

    monkeypatch.setattr(ConfiguredArtifactMobilityPeer, 'recipient_attestation', interrupted)
    with pytest.raises(MobilityJournalError, match='UNAVAILABLE'):
        binding.send(record['source_artifact_id'], confirm_destination='node-one')
    reopened = ArtifactMobilityBinding(lambda: config)
    rows = reopened.list()
    assert len(rows) == 1
    assert rows[0]['state'] == 'dispatching'
    operation = reopened._repository_for_dispatch().load_operation(rows[0]['operation_id'], 'owner-a')
    assert operation.lease_expires_at > time.time()
    assert operation.lease_token is not None
    reopened.close()


def test_source_only_application_has_no_outbound_capability_or_mutation_surface(tmp_path):
    from sonder_runtime.bootstrap.app import build_application
    config = config_for(tmp_path)
    config = replace(config, artifact_mobility=ArtifactMobilityConfig())
    application = build_application(config=config)
    assert application.artifact_mobility_list() == {'outcome_code': 'UNAVAILABLE'}
    assert not application.operational_capabilities()['mobility']['fixed_peer_artifact_copy']['available']
    for name in ('artifact_mobility_send', 'artifact_mobility_resume', 'artifact_mobility_publish', 'artifact_mobility_source'):
        assert not hasattr(application, name)
    application.close_artifact_mobility()
    assert not (tmp_path / 'private-source').exists()


def test_binding_rejects_generic_source_callbacks(tmp_path):
    from sonder_runtime.bootstrap.artifact_mobility import ArtifactMobilityBinding, compose_artifact_mobility
    for constructor in (ArtifactMobilityBinding, compose_artifact_mobility):
        with pytest.raises(TypeError, match='host-owned'):
            constructor(lambda: config_for(tmp_path), source_binding=lambda: object())


def test_enabled_outbound_config_http_startup_has_no_controller_or_publisher(tmp_path, monkeypatch):
    from sonder_runtime.bootstrap import artifact_mobility
    from sonder_runtime.interfaces.http import serve
    from http.server import ThreadingHTTPServer
    import threading
    import urllib.error
    import urllib.request
    config = config_for(tmp_path)
    monkeypatch.setattr(artifact_mobility, 'ArtifactMobilityBinding', lambda *a, **k: pytest.fail('outbound constructed'))
    # configure_typed_config owns existing receiver/admin globals only.
    for name in ('_ARTIFACT_TRANSFER_BINDING', '_ARTIFACT_TRANSFER_CONFIG',
        '_APP_CONTROL_BINDING', '_APP_CONTROL_CONFIG', 'CONFIGURED_PORT', 'API_KEY',
        'AUTH_SECRET', 'HOST', 'REQUIRE_ACCOUNT', 'AUTH_MODE', 'CORS_ORIGINS',
        'TLS_TERMINATED_BY_PROXY', 'ALLOW_REGISTRATION', 'MAX_REQUEST_BYTES',
        'MAX_DISCARDED_BODY_BYTES', 'REQUEST_TIMEOUT_SECONDS', 'STREAM_IDLE_TIMEOUT_SECONDS',
        'HTTP_SESSION_STATE_LIMIT', 'HTTP_SESSION_STATE_OWNER_LIMIT', 'TRAIN_MAX_N',
        '_HEALTH_STATUS_FACADE', '_TRUSTED_PROXY_NETWORKS'):
        monkeypatch.setattr(serve, name, getattr(serve, name))
    monkeypatch.setattr(serve, '_ARTIFACT_TRANSFER_BINDING', None)
    monkeypatch.setattr(serve, '_APP_CONTROL_BINDING', None)
    serve.configure_typed_config(config)
    assert serve._ARTIFACT_TRANSFER_BINDING._service is None
    assert '_ARTIFACT_MOBILITY_BINDING' not in vars(serve)
    assert '_ARTIFACT_MOBILITY_SOURCE_BINDING' not in vars(serve)
    assert not (tmp_path / 'private-source').exists()
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    origin = f'http://127.0.0.1:{httpd.server_address[1]}'
    try:
        for method, path in (
            ('GET', '/v1/artifact-mobility/list'),
            ('GET', '/v1/artifact-mobility/status/' + 'a' * 32),
            ('POST', '/v1/artifact-mobility/send'),
            ('POST', '/v1/artifact-mobility/resume'),
            ('POST', '/v1/artifact-mobility-sources/publish'),
            ('POST', '/v1/artifact-mobility-source/stage')):
            request = urllib.request.Request(origin + path,
                data=b'{}' if method == 'POST' else None, method=method,
                headers={'Content-Type': 'application/json'})
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=5)
            assert error.value.code == 404
            assert json.loads(error.value.read())['error']['type'] == 'not_found'
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(5)
        serve._ARTIFACT_TRANSFER_BINDING.close()
    assert not (tmp_path / 'private-source').exists()


@pytest.mark.parametrize('field,value', [
    ('destination_origin', 'https://another.example:9443'),
    ('destination_tls_certificate_sha256', 'd' * 64),
    ('expected_recipient_attestation_sha256', 'e' * 64),
    ('destination_credential_id', 'generation-two'),
    ('destination_label', 'renamed-display'),
    ('peer_key', 'rotated-private-key-' + 'f' * 32),
    ('source_owner_id', 'changed-owner'),
    ('project_id', 'changed-project'),
])
def test_existing_operation_binding_change_is_terminal_before_peer(tmp_path, monkeypatch, field, value):
    from sonder_runtime.bootstrap.artifact_mobility import ArtifactMobilityBinding
    from sonder_runtime.adapters.compute_fabric.artifact_mobility import ConfiguredArtifactMobilityPeer
    from sonder_runtime.application.artifacts.transfer import TransferError
    config = config_for(tmp_path)
    current = [config]
    source, record = trusted_source(config)
    binding = ArtifactMobilityBinding(lambda: current[0], source_binding=source)
    calls = []
    def unavailable(self, spec):
        calls.append('attestation')
        raise TransferError('MOBILITY_UNAVAILABLE')
    monkeypatch.setattr(ConfiguredArtifactMobilityPeer, 'recipient_attestation', unavailable)
    first = binding.send(record['source_artifact_id'], confirm_destination='node-one')
    assert first['state'] == 'retryable_blocked'
    if field == 'peer_key':
        current[0] = replace(config, secrets=replace(config.secrets, artifact_mobility_peer_key=value))
    elif field in ('source_owner_id', 'project_id'):
        current[0] = replace(config, artifact_mobility_source=replace(config.artifact_mobility_source, **{field: value}))
    else:
        current[0] = replace(config, artifact_mobility=replace(config.artifact_mobility, **{field: value}))
    resumed = binding.resume(first['operation_id'])
    assert resumed['state'] == 'terminal_blocked'
    assert resumed['outcome_code'] == 'IMMUTABLE_FENCE'
    assert calls == ['attestation']
    binding.close()


def test_default_application_shutdown_closes_only_existing_mobility(tmp_path, monkeypatch):
    from sonder_runtime.bootstrap import app
    config = config_for(tmp_path)
    source, record = trusted_source(config)
    application = app.build_application(config=config, _artifact_mobility_source_binding=source)
    for name in ('_default_config', '_default_compute_close', '_default_delegation_close', '_default_artifact_mobility_close'):
        monkeypatch.setattr(app, name, getattr(app, name))
    monkeypatch.setattr(app._application_lifecycle, 'get', lambda: application)
    assert app.default_app() is application
    assert not source._closed
    app.close_default_runtime_resources()
    assert source._closed
    assert application.artifact_mobility_list() == {'outcome_code': 'UNAVAILABLE'}


def test_receipt_binding_keeps_owner_mismatch_value_free_and_read_only(tmp_path):
    from pathlib import Path
    from sonder_runtime.bootstrap.artifact_mobility import compose_artifact_mobility
    from sonder_runtime.application.artifacts.mobility import ArtifactMobilityJournal
    from tests.test_artifact_mobility_persistence import _request

    config = config_for(tmp_path)
    current = [config]
    get_binding, status, listing, _available, close = compose_artifact_mobility(lambda: current[0])
    try:
        assert listing() == {"operations": []}
        repository = get_binding()._repository_for_dispatch()
        journal = ArtifactMobilityJournal(repository)
        operation = journal.create_operation(_request(source_owner_id="owner-a"),
            credential_material="c" * 48)
        expected = status(operation.operation_id)
        assert expected["state"] == "ready"
        assert listing() == {"operations": [expected]}
        root = Path(config.artifact_mobility_source.store_dir) / "outbound-journal"
        before = {p.name: p.read_bytes() for p in root.iterdir()}
        current[0] = replace(config, artifact_mobility_source=replace(
            config.artifact_mobility_source, source_owner_id="owner-b"))
        assert listing() == {"outcome_code": "UNAVAILABLE"}
        assert status(operation.operation_id) == {"outcome_code": "UNAVAILABLE"}
        assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    finally:
        close()


@pytest.mark.parametrize(("table", "column"), (
    ("mobility_journal_owner", "source_owner_id"),
    ("mobility_operations", "receiver_artifact_id"),
    ("mobility_tombstones", "terminal_at"),
))
def test_receipt_inspection_rejects_missing_columns_without_repair(tmp_path, table, column):
    import sqlite3
    from pathlib import Path
    from sonder_runtime.adapters.persistence.artifact_mobility import SQLiteArtifactMobilityJournal
    from sonder_runtime.bootstrap.artifact_mobility import compose_artifact_mobility

    config = config_for(tmp_path)
    root = Path(config.artifact_mobility_source.store_dir) / "outbound-journal"
    SQLiteArtifactMobilityJournal(root).close()
    with sqlite3.connect(root / "artifact-mobility.sqlite") as connection:
        connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    connection.close()
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    _binding, status, listing, _available, close = compose_artifact_mobility(lambda: config)
    try:
        assert listing() == {"outcome_code": "UNAVAILABLE"}
        assert status("f" * 32) == {"outcome_code": "UNAVAILABLE"}
        assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    finally:
        close()
