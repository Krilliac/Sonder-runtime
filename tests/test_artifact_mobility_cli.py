import json
import pytest
from sonder_runtime import __main__ as cli


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
    monkeypatch.setattr(cli, '_load_config', lambda args: (_ for _ in ()).throw(ValueError('https://private.invalid:9443 SECRET')))
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
    from sonder_runtime.bootstrap import app
    from sonder_runtime.adapters.compute_fabric.artifact_mobility import ConfiguredArtifactMobilityPeer
    from sonder_runtime.application.artifacts.transfer import TransferError
    from tests.test_artifact_mobility_binding import config_for, trusted_source
    config = config_for(tmp_path)
    source, record = trusted_source(config)
    build_application = app.build_application
    application = build_application(config=config, _artifact_mobility_source_binding=source)
    monkeypatch.setattr(ConfiguredArtifactMobilityPeer, 'recipient_attestation',
        lambda self, spec: (_ for _ in ()).throw(TransferError('peer-private-message')))
    monkeypatch.setattr(cli, '_load_config', lambda args: config)
    monkeypatch.setattr(app, 'build_application', lambda **kwargs: application)
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
    monkeypatch.setattr(app, 'build_application', build_application)
    assert cli.main(['artifact-mobility', 'status', '--operation-id', receipt['operation_id'], '--json']) == 0
    assert json.loads(capsys.readouterr().out) == receipt
    assert cli.main(['artifact-mobility', 'list', '--json']) == 0
    assert json.loads(capsys.readouterr().out) == {'operations': [receipt]}


def test_default_cli_has_no_trusted_publisher_and_never_constructs_peer(tmp_path, monkeypatch, capsys):
    from tests.test_artifact_mobility_binding import config_for
    from sonder_runtime.adapters.compute_fabric import artifact_mobility
    config = config_for(tmp_path)
    monkeypatch.setattr(cli, '_load_config', lambda args: config)
    monkeypatch.setattr(artifact_mobility, 'ConfiguredArtifactMobilityPeer',
        lambda *args, **kwargs: pytest.fail('peer constructed'))
    assert cli.main(['artifact-mobility', 'send', '--source-artifact', 'a' * 32,
        '--confirm-destination', 'node-one', '--json']) == 2
    assert json.loads(capsys.readouterr().out) == {'outcome_code': 'SOURCE_UNAVAILABLE'}
    assert not (tmp_path / 'private-source').exists()
