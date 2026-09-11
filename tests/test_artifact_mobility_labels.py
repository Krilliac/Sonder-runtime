"""Public destination labels must be opaque before host access or projection."""

from dataclasses import replace
import json

import pytest

from sonder_runtime import __main__ as cli
from sonder_runtime.application.artifacts.mobility import MobilityJournalError
from sonder_runtime.platform.config import ConfigError, load_config
from tests.test_artifact_mobility_cli import no_mobility_host_access
from tests.test_artifact_mobility_config import _enabled_toml
from tests.test_artifact_mobility_persistence import _operation, _request
from tests.test_artifact_mobility_service import _context


UNSAFE_LABELS = [
    'https://PRIVATE.invalid:9443', 'C:/PRIVATE/source', 'C:\\PRIVATE', '/PRIVATE',
    '../PRIVATE', 'node.example', '.', '..', 'node:9443', '', 'a' * 129,
    'node\N{EM DASH}b', '\N{FULLWIDTH LATIN SMALL LETTER N}ode-b', 'node b', 'node\nb',
    'node\x00b', '-node-b', 'node%2Fb',
]


@pytest.mark.parametrize('label', UNSAFE_LABELS)
@pytest.mark.parametrize('joined', [False, True])
@pytest.mark.parametrize('help_position', [None, 1, 2, -1])
def test_confirmation_label_rejected_before_host_or_help(
        label, joined, help_position, no_mobility_host_access, capsys):
    args = ['artifact-mobility', 'send', '--source-artifact', 'a' * 32]
    args.extend(['--confirm-destination=' + label] if joined else ['--confirm-destination', label])
    if help_position is not None:
        args.insert(len(args) if help_position == -1 else help_position, '--help')
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == 'artifact-mobility: INVALID_REQUEST\n'


@pytest.mark.parametrize('label', UNSAFE_LABELS)
def test_configuration_rejects_unsafe_public_label_without_echo(tmp_path, label):
    path = tmp_path / 'sonder.toml'
    path.write_text(_enabled_toml(tmp_path, 'https://node-b.example:9443').replace(
        'destination_label = "node-b"', 'destination_label = ' + json.dumps(label)), encoding='utf-8')
    with pytest.raises(ConfigError) as error:
        load_config(path, env={'SONDER_ARTIFACT_MOBILITY_PEER_KEY': 'mobility-' + 'x' * 32})
    assert error.value.errors == ('[artifact_mobility].destination_label invalid',)


@pytest.mark.parametrize('label', UNSAFE_LABELS)
def test_application_rejects_unsafe_labels_before_retention_or_projection(tmp_path, label):
    for construct in (_context, _request):
        with pytest.raises(MobilityJournalError, match='^INVALID_REQUEST$'):
            construct(destination_label=label)
    _, _, operation = _operation(tmp_path)
    with pytest.raises(MobilityJournalError, match='^INTEGRITY$'):
        replace(operation, destination_label=label)


def test_persisted_unsafe_label_cannot_escape_status_or_list(tmp_path):
    repository, _, operation = _operation(tmp_path)
    with repository._connection() as connection:
        connection.execute('UPDATE mobility_operations SET destination_label=?',
            ('https://PRIVATE.invalid:9443',))
    with pytest.raises(MobilityJournalError, match='^INTEGRITY$'):
        repository.public_status(operation.operation_id, operation.source_owner_id)
    with pytest.raises(MobilityJournalError, match='^INTEGRITY$'):
        repository.list_public_status(operation.source_owner_id)


@pytest.mark.parametrize('label', ['node-b', 'Node_B-2', 'a', 'a' * 128])
def test_safe_public_label_agrees_across_config_cli_and_receipt(tmp_path, label):
    path = tmp_path / 'sonder.toml'
    path.write_text(_enabled_toml(tmp_path, 'https://node-b.example:9443').replace(
        'destination_label = "node-b"', 'destination_label = ' + json.dumps(label)), encoding='utf-8')
    config = load_config(path, env={'SONDER_ARTIFACT_MOBILITY_PEER_KEY': 'mobility-' + 'x' * 32})
    assert config.artifact_mobility.destination_label == label
    args = cli.build_parser().parse_args(['artifact-mobility', 'send', '--source-artifact',
        'a' * 32, '--confirm-destination', label])
    assert args.confirm_destination == label
    assert _context(destination_label=label).destination_label == label
    repository, _, operation = _operation(tmp_path, destination_label=label)
    assert operation.public_status()['destination_label'] == label
    assert repository.public_status(operation.operation_id, operation.source_owner_id)['destination_label'] == label
    assert repository.list_public_status(operation.source_owner_id)[0]['destination_label'] == label
