import json
import pytest
from sonder_runtime import __main__ as cli


@pytest.fixture(autouse=True)
def _isolated_host_provisioning(tmp_path, monkeypatch):
    from sonder_runtime.bootstrap import app, artifact_mobility
    app.reset_for_tests()
    resolver = artifact_mobility._canonical_mobility_host_home
    # Substitute only the OS account directory boundary for this test user.
    # Application construction and canonical config loading stay production.
    monkeypatch.setattr(artifact_mobility, '_canonical_mobility_host_home',
        lambda: tmp_path / 'host-provisioning')
    try:
        yield resolver
    finally:
        app.reset_for_tests()


def _provision_host(config):
    from dataclasses import asdict
    from sonder_runtime.bootstrap.artifact_mobility import _canonical_mobility_host_home
    home = _canonical_mobility_host_home()
    home.mkdir(parents=True)
    lines = []
    for name, values in (
        ('state', {'home': config.state.home}),
        ('artifact_mobility_source', asdict(config.artifact_mobility_source)),
        ('artifact_mobility', asdict(config.artifact_mobility)),
    ):
        lines.append('[' + name + ']')
        lines.extend(key + ' = ' + json.dumps(value) for key, value in values.items())
    (home / 'sonder.toml').write_text('\n'.join(lines), encoding='utf-8')
    secret = home / 'sonder.env'
    secret.write_text('SONDER_ARTIFACT_MOBILITY_PEER_KEY=' + config.secrets.artifact_mobility_peer_key + '\n', encoding='utf-8')
    secret.chmod(0o600)
    return home


def test_only_narrow_cli_commands_and_no_caller_operation_id(capsys):
    parser = cli.build_parser()
    for action, extra in [('send', ['--source-artifact', 'a' * 32, '--confirm-destination', 'node-one']),
                          ('resume', ['--operation-id', 'a' * 32]),
                          ('status', ['--operation-id', 'a' * 32]), ('list', [])]:
        args = parser.parse_args(['artifact-mobility', action, *extra])
        assert args.mobility_command == action
    for extra in (['send', '--source-artifact', 'a' * 32, '--confirm-destination', 'node-one', '--operation-id', 'b' * 32],
                  ['stage', '--path', 'secret'], ['send', '--url', 'https://private.invalid:9443']):
        with pytest.raises(SystemExit):
            parser.parse_args(['artifact-mobility', *extra])


def test_cli_error_detail_is_never_emitted(monkeypatch, capsys):
    from sonder_runtime.bootstrap import artifact_mobility
    monkeypatch.setattr(artifact_mobility, '_load_mobility_host_config', lambda: (_ for _ in ()).throw(ValueError('https://private.invalid:9443 SECRET')))
    assert cli.main(['artifact-mobility', 'list', '--json']) == 2
    output = capsys.readouterr()
    assert json.loads(output.out) == {'outcome_code': 'UNAVAILABLE'}
    assert output.err == ''


def test_repl_rejects_mutations_without_constructing_application(monkeypatch):
    from sonder_runtime.interfaces.repl import repl
    monkeypatch.setattr(repl.server, '_application', lambda: pytest.fail('application accessed'))
    for command in ('send --source-artifact secret', 'resume aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'stage secret'):
        assert json.loads(repl._artifact_mobility_command(command)) == {'outcome_code': 'INVALID_REQUEST'}


@pytest.mark.parametrize('args', [
    ['send', '--source-artifact', 'a' * 32, '--confirm-destination', 'label', '--url', 'https://PRIVATE.invalid:9443'],
    ['https://PRIVATE.invalid:9443'],
    ['status', '--operation-id'],
])
def test_cli_argument_errors_do_not_echo_values(args, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(['artifact-mobility', *args])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert 'PRIVATE' not in output.err + output.out
    assert 'INVALID_REQUEST' in output.err


def test_cli_uses_same_receipt_projection(tmp_path, monkeypatch, capsys):
    from sonder_runtime.adapters.compute_fabric.artifact_mobility import ConfiguredArtifactMobilityPeer
    from sonder_runtime.application.artifacts.transfer import TransferError
    from tests.test_artifact_mobility_binding import config_for, trusted_source
    config = config_for(tmp_path)
    source, record = trusted_source(config)
    source.close()
    _provision_host(config)
    monkeypatch.setattr(ConfiguredArtifactMobilityPeer, 'recipient_attestation',
        lambda self, spec: (_ for _ in ()).throw(TransferError('peer-private-message')))
    monkeypatch.setattr(cli, '_load_config', lambda args: pytest.fail('caller loader used'))
    result = cli.main(['artifact-mobility', 'send', '--source-artifact', record['source_artifact_id'],
        '--confirm-destination', 'node-one', '--json'])
    assert result == 0
    output = capsys.readouterr()
    receipt = json.loads(output.out)
    assert receipt['state'] == 'terminal_blocked'
    assert set(receipt) == {'operation_id', 'source_artifact_id', 'destination_label',
        'state', 'outcome_code', 'created_at', 'updated_at'}
    assert 'peer-private-message' not in output.out + output.err
    assert config.artifact_mobility.destination_origin not in output.out + output.err
    assert cli.main(['artifact-mobility', 'status', '--operation-id', receipt['operation_id'], '--json']) == 0
    assert json.loads(capsys.readouterr().out) == receipt
    assert cli.main(['artifact-mobility', 'list', '--json']) == 0
    assert json.loads(capsys.readouterr().out) == {'operations': [receipt]}


def test_disabled_host_cli_never_constructs_peer(tmp_path, monkeypatch, capsys):
    from dataclasses import replace
    from tests.test_artifact_mobility_binding import config_for
    from sonder_runtime.platform.artifact_mobility_config import ArtifactMobilityConfig
    from sonder_runtime.platform.artifact_mobility_source_config import ArtifactMobilitySourceConfig
    from sonder_runtime.adapters.compute_fabric import artifact_mobility
    config = config_for(tmp_path)
    config = replace(config, artifact_mobility=ArtifactMobilityConfig(),
        artifact_mobility_source=ArtifactMobilitySourceConfig())
    _provision_host(config)
    monkeypatch.setattr(artifact_mobility, 'ConfiguredArtifactMobilityPeer',
        lambda *args, **kwargs: pytest.fail('peer constructed'))
    assert cli.main(['artifact-mobility', 'send', '--source-artifact', 'a' * 32,
        '--confirm-destination', 'node-one', '--json']) == 2
    assert json.loads(capsys.readouterr().out) == {'outcome_code': 'UNAVAILABLE'}
    assert not (tmp_path / 'private-source').exists()


@pytest.mark.parametrize('prefix', [[], ['--config', 'C:/PRIVATE.toml'], ['--secrets=C:/PRIVATE.env'],
    ['--bogus', 'https://PRIVATE.invalid:9443'], ['PRIVATE-value']])
@pytest.mark.parametrize('extra', [[], ['--config', 'C:/PRIVATE.toml'], ['--secrets', 'C:/PRIVATE.env'],
    ['--set', 'artifact_mobility.destination_origin=https://PRIVATE.invalid:9443'],
    ['--destination-url', 'https://PRIVATE.invalid:9443'], ['--pin', 'PRIVATE-pin'],
    ['--key', 'PRIVATE-key'], ['--grant', 'PRIVATE-grant']])
def test_mobility_override_rejection_precedes_all_host_access(prefix, extra, monkeypatch, capsys):
    if not prefix and not extra:
        return
    from sonder_runtime.bootstrap import app
    monkeypatch.setattr(cli, '_load_config', lambda *args: pytest.fail('caller config loaded'))
    monkeypatch.setattr(app, 'build_application', lambda *args, **kwargs: pytest.fail('application composed'))
    with pytest.raises(SystemExit) as failure:
        cli.main([*prefix, 'artifact-mobility', 'list', *extra])
    assert failure.value.code == 2
    output = capsys.readouterr()
    assert 'PRIVATE' not in output.out + output.err
    assert output.err == 'artifact-mobility: INVALID_REQUEST\n'


def test_direct_parser_redacts_misplaced_mobility_arguments(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(['--bogus', 'https://PRIVATE.invalid:9443', 'artifact-mobility', 'list'])
    assert capsys.readouterr().err == 'artifact-mobility: INVALID_REQUEST\n'


@pytest.fixture
def no_mobility_host_access(monkeypatch):
    from sonder_runtime.bootstrap import app, artifact_mobility

    def forbidden(*args, **kwargs):
        pytest.fail('rejected argv accessed host authority')

    monkeypatch.setattr(app, '_artifact_mobility_operator_application', forbidden)
    monkeypatch.setattr(app, 'build_application', forbidden)
    monkeypatch.setattr(artifact_mobility, '_load_mobility_host_config', forbidden)
    monkeypatch.setattr(artifact_mobility, '_canonical_mobility_host_home', forbidden)
    monkeypatch.setattr(cli, '_load_config', forbidden)


@pytest.mark.parametrize('family', [
    'ARTIFACT-MOBILITY', 'Artifact-Mobility', 'aRtIfAcT-mObIlItY', 'artifact-MOBILITY',
])
@pytest.mark.parametrize('before, after', [
    ([], ['list']),
    (['--config', 'C:/PRIVATE.toml'], ['list']),
    (['--bogus', 'https://PRIVATE.invalid:9443'], ['list']),
    ([], ['list', '--config', 'C:/PRIVATE.toml']),
    ([], ['--bogus', 'https://PRIVATE.invalid:9443', 'list']),
    (['PRIVATE-before'], ['list', 'PRIVATE-after']),
    (['--help', '--config=C:/PRIVATE.toml'], ['list']),
    ([], ['--help', 'list', '--secrets=C:/PRIVATE.env']),
])
def test_case_variant_mobility_family_redacts_all_token_orders(
        family, before, after, no_mobility_host_access, capsys):
    with pytest.raises(SystemExit) as failure:
        cli.main([*before, family, *after])
    assert failure.value.code == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == 'artifact-mobility: INVALID_REQUEST\n'


@pytest.mark.parametrize('action, option', [
    ('send', '--source-artifact'), ('resume', '--operation-id'), ('status', '--operation-id'),
])
@pytest.mark.parametrize('value', [
    'C:/PRIVATE/source.bin', 'https://PRIVATE.invalid:9443', '../PRIVATE', '',
    'a' * 31, 'a' * 33, 'a' * 4096, 'A' * 32, 'g' * 32, '0' * 31 + '\N{ARABIC-INDIC DIGIT ZERO}',
    ' ' + 'a' * 32, 'a' * 32 + '\n', 'a' * 15 + '\x00' + 'a' * 16,
])
@pytest.mark.parametrize('joined', [False, True])
@pytest.mark.parametrize('help_position', [None, 'family', 'action', 'tail'])
def test_malformed_mobility_ids_rejected_before_host_and_help(
        action, option, value, joined, help_position, no_mobility_host_access, capsys):
    args = ['artifact-mobility', action]
    args.extend([option + '=' + value] if joined else [option, value])
    if action == 'send':
        args.extend(['--confirm-destination', 'node-one'])
    if help_position is not None:
        args.insert({'family': 1, 'action': 2, 'tail': len(args)}[help_position], '--help')
    with pytest.raises(SystemExit) as failure:
        cli.main(args)
    assert failure.value.code == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == 'artifact-mobility: INVALID_REQUEST\n'


@pytest.mark.parametrize('selector', [
    '--config', '--secrets', '--set', '--destination-url', '--pin', '--key', '--grant', '--unknown',
])
@pytest.mark.parametrize('joined', [False, True])
@pytest.mark.parametrize('position', range(5))
def test_forbidden_mobility_selectors_rejected_in_every_help_position(
        selector, joined, position, no_mobility_host_access, capsys):
    args = ['artifact-mobility', '--help', 'list', '--help']
    override = [selector + '=PRIVATE'] if joined else [selector, 'PRIVATE']
    args[position:position] = override
    with pytest.raises(SystemExit) as failure:
        cli.main(args)
    assert failure.value.code == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == 'artifact-mobility: INVALID_REQUEST\n'


@pytest.mark.parametrize('args', [
    ['--help'], ['-h'], ['send', '--help'], ['resume', '-h'], ['status', '--help'], ['list', '--help'],
    ['send', '--help', '--source-artifact', '0123456789abcdef' * 2, '--confirm-destination', 'node-one'],
    ['resume', '--operation-id=' + '0123456789abcdef' * 2, '--help'],
])
def test_clean_mobility_help_stays_available(args, no_mobility_host_access, capsys):
    with pytest.raises(SystemExit) as result:
        cli.main(['artifact-mobility', *args])
    assert result.value.code == 0
    output = capsys.readouterr()
    assert 'usage:' in output.out
    assert output.err == ''


def test_production_cli_reopens_preadmitted_source_and_resumes_without_constructor_mock(tmp_path, monkeypatch, capsys):
    """Real CLI/Application/source/journal/peer/receiver; scripted transport, no TLS proof."""
    from dataclasses import replace
    from sonder_runtime.bootstrap import app
    from sonder_runtime.adapters.compute_fabric.http_client import PinnedHttpsClient, PinnedHttpsResponse, PinnedHttpsClientError
    from sonder_runtime.interfaces.http.facades.artifact_transfer import dispatch_artifact_transfer
    from tests.test_artifact_mobility_binding import config_for, trusted_source
    from tests.test_artifact_transfer_mobility_protocol import _receiver
    receiver, context = _receiver(tmp_path / 'destination')
    config = config_for(tmp_path / 'source')
    config = replace(config,
        artifact_mobility_source=replace(config.artifact_mobility_source, source_owner_id='source-owner-a'),
        artifact_mobility=replace(config.artifact_mobility,
            expected_recipient_attestation_sha256=receiver.mobility_attestation(context)['sha256']),
        secrets=replace(config.secrets, artifact_mobility_peer_key='transfer-' + 'b' * 32))
    producer, artifact = trusted_source(config)
    producer.close()
    lost = [False]
    commands = []
    calls = []
    service = receiver.service()

    def request(self, method, path, *, body, headers_supplier, response_limit):
        headers = headers_supplier()
        assert headers['Authorization'] == 'Bearer transfer-' + 'b' * 32
        calls.append((method, path))
        if method == 'GET':
            return PinnedHttpsResponse(200, json.dumps(receiver.mobility_attestation(context)).encode())
        contract = receiver.mobility_contract(context, headers['X-Sonder-Artifact-Mobility-Receipt-Capability'])
        if method == 'PUT':
            fields = path.split('/')
            result = dispatch_artifact_transfer(service, 'append', {
                'transfer_id': fields[3], 'offset': int(fields[5]),
                'chunk_sha256': headers['X-Sonder-Chunk-Sha256']}, context, body=body, mobility=contract)
            if not lost[0]:
                lost[0] = True
                raise PinnedHttpsClientError('MOBILITY_UNAVAILABLE')
        elif path == '/v1/artifact-transfers':
            payload = json.loads(body)
            commands.append(payload['command_id'])
            result = dispatch_artifact_transfer(service, 'begin', payload, context, mobility=contract)
        else:
            fields = path.split('/')
            payload = {'transfer_id': fields[3], **json.loads(body)}
            action = 'seal' if fields[4] == 'seal' else 'mobility_receipt'
            result = dispatch_artifact_transfer(service, action, payload, context, mobility=contract)
            if action == 'seal':
                # Synchronize the receiver's existing verifier, not sender polling.
                service._workers.shutdown(wait=True)
                result = dispatch_artifact_transfer(service, 'mobility_receipt',
                    {'transfer_id': fields[3], 'command_id': commands[0]}, context, mobility=contract)
        return PinnedHttpsResponse(result.status_code, json.dumps(result.body).encode())

    monkeypatch.setattr(PinnedHttpsClient, 'request', request)
    app.reset_for_tests()
    try:
        # Real canonical host files select authority; only the OS account root
        # is isolated by the fixture. No loader/constructor is replaced.
        _provision_host(config)
        assert cli.main(['artifact-mobility', 'send', '--source-artifact', artifact['source_artifact_id'],
            '--confirm-destination', 'node-one', '--json']) == 0
        first = json.loads(capsys.readouterr().out)
        assert first['state'] == 'resumable'
        assert first['source_artifact_id'] == artifact['source_artifact_id']
        app.reset_for_tests()
        assert cli.main(['artifact-mobility', 'resume', '--operation-id', first['operation_id'], '--json']) == 0
        final = json.loads(capsys.readouterr().out)
        assert final['state'] == 'sealed'
        assert final['operation_id'] == first['operation_id']
        assert len(commands) == 1
        assert len([call for call in calls if call[0] == 'PUT']) == 1
        assert cli.main(['artifact-mobility', 'status', '--operation-id', first['operation_id'], '--json']) == 0
        assert json.loads(capsys.readouterr().out) == final
        assert cli.main(['artifact-mobility', 'list', '--json']) == 0
        assert json.loads(capsys.readouterr().out) == {'operations': [final]}
        binding = app.default_app()._artifact_mobility_binding()
        record = binding._repository_for_dispatch().load_operation(first['operation_id'], 'source-owner-a')
        # Verify the receiver's private durable bytes directly in this test;
        # the protocol correctly denies general artifact reads of mobility rows.
        with service.store._connection() as connection:
            row = connection.execute('SELECT * FROM artifact_uploads WHERE id=?',
                (record.receipt.transfer_id,)).fetchone()
        assert row['state'] == 'sealed'
        with service.store._directories(row) as (_, stage):
            assert stage.read_bytes(artifact['sha256'], max_bytes=artifact['size_bytes']) == b'pre-admitted source'
    finally:
        app.reset_for_tests()
        receiver.close()


def test_first_start_uses_only_canonical_provisioned_files_and_pins_graph(tmp_path, monkeypatch, capsys):
    from sonder_runtime.bootstrap import app, artifact_mobility
    from tests.test_artifact_mobility_binding import config_for
    config = config_for(tmp_path)
    home = _provision_host(config)
    for name in ('SONDER_CONFIG', 'SONDER_SECRETS', 'SONDER_HOME', 'SONDER_STATE_HOME'):
        monkeypatch.setenv(name, 'C:/PRIVATE-alternate-authority')
    monkeypatch.setenv('SONDER_ARTIFACT_MOBILITY_PEER_KEY', 'https://PRIVATE.invalid:9443/key')
    monkeypatch.setattr(cli, '_load_config', lambda args: pytest.fail('shared per-command loader used'))
    assert cli.main(['artifact-mobility', 'list', '--json']) == 0
    assert json.loads(capsys.readouterr().out) == {'operations': []}
    owned = app.default_app()
    assert owned.config.artifact_mobility == config.artifact_mobility
    assert owned.config.artifact_mobility_source == config.artifact_mobility_source
    assert owned.config.secrets.artifact_mobility_peer_key == config.secrets.artifact_mobility_peer_key
    assert owned.config.state.home == config.state.home
    assert owned.operational_capabilities()['mobility']['fixed_peer_artifact_copy']['available']
    (home / 'sonder.toml').write_text('INVALID NEW CONFIG', encoding='utf-8')
    monkeypatch.setattr(artifact_mobility, '_load_mobility_host_config', lambda: pytest.fail('host authority reloaded'))
    assert cli.main(['artifact-mobility', 'list', '--json']) == 0
    assert json.loads(capsys.readouterr().out) == {'operations': []}
    assert app.default_app() is owned


def test_unprovisioned_host_fails_before_application_construction(monkeypatch, capsys):
    from sonder_runtime.bootstrap import app
    monkeypatch.setattr(app, 'build_application', lambda **kwargs: pytest.fail('unprovisioned graph constructed'))
    assert cli.main(['artifact-mobility', 'list', '--json']) == 2
    assert json.loads(capsys.readouterr().out) == {'outcome_code': 'UNAVAILABLE'}


def test_first_start_rejects_missing_explicit_host_state_home(tmp_path, capsys):
    from tests.test_artifact_mobility_binding import config_for
    home = _provision_host(config_for(tmp_path))
    text = (home / 'sonder.toml').read_text(encoding='utf-8')
    (home / 'sonder.toml').write_text('\n'.join(line for line in text.splitlines() if not line.startswith('home =')), encoding='utf-8')
    assert cli.main(['artifact-mobility', 'list', '--json']) == 2
    assert json.loads(capsys.readouterr().out) == {'outcome_code': 'UNAVAILABLE'}


def test_os_provisioning_root_ignores_environment_selectors(_isolated_host_provisioning, monkeypatch):
    resolve = _isolated_host_provisioning
    expected = resolve()
    for name in ('SONDER_CONFIG', 'SONDER_SECRETS', 'SONDER_HOME', 'SONDER_STATE_HOME'):
        monkeypatch.setenv(name, 'C:/PRIVATE-alternate-authority')
    assert resolve() == expected
