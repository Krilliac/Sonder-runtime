import json
from dataclasses import replace
import os
import time

import pytest

from sonder_runtime.platform.artifact_mobility_config import ArtifactMobilityConfig
from sonder_runtime.platform.artifact_mobility_source_config import (
    ArtifactMobilitySourceConfig,
    source_scope_id,
)
from sonder_runtime.platform.config import (
    ConfigError,
    Secrets,
    SonderConfig,
    StateConfig,
    load_config,
)
from sonder_runtime.platform import config_environment


def test_mobility_sections_and_peer_secret_are_disabled_by_default():
    config = SonderConfig()

    assert config.artifact_mobility == ArtifactMobilityConfig()
    assert config.artifact_mobility_source == ArtifactMobilitySourceConfig()
    assert config.artifact_mobility.enabled is False
    assert config.artifact_mobility_source.enabled is False
    assert config.secrets.artifact_mobility_peer_key == ""
    assert config.as_redacted_dict()["secrets"]["artifact_mobility_peer_key"] == "[unset]"


def _enabled_toml(tmp_path, origin, *, source_store=None):
    source_store = source_store or tmp_path / "export-spool"
    return (
        "[artifact_mobility_source]\n"
        "enabled = true\n"
        f"store_dir = {json.dumps(str(source_store))}\n"
        'principal_id = "private-cluster"\n'
        'project_id = "sonder"\n'
        'source_owner_id = "node-a"\n'
        "\n"
        "[artifact_mobility]\n"
        "enabled = true\n"
        'destination_label = "node-b"\n'
        f"destination_origin = {json.dumps(origin)}\n"
        f'destination_tls_certificate_sha256 = {"a" * 64!r}\n'
        f'expected_recipient_attestation_sha256 = {"b" * 64!r}\n'
        'destination_credential_id = "node-b-key-v1"\n'
    )


def _enabled_memory_replication_toml() -> str:
    return '''
[memory_replication]
enabled = true
local_node_id = "node-a"
project_scope = "sonder"
receiver_enabled = false
request_timeout_seconds = 5
max_request_bytes = 8192
max_response_bytes = 4096
max_batch_records = 16

[[memory_replication.peers]]
node_id = "node-c"
project_scope = "sonder"
origin = "https://node-c.example:8443"
'''


def test_enabled_mobility_rejects_invalid_destination_origin_without_echo(tmp_path):
    rejected_origin = "http://secret-user:leaked-value@node-b.example:9443/path?token=leak"
    path = tmp_path / "sonder.toml"
    path.write_text(_enabled_toml(tmp_path, rejected_origin), encoding="utf-8")

    with pytest.raises(ConfigError) as raised:
        load_config(
            path,
            env={"SONDER_ARTIFACT_MOBILITY_PEER_KEY": "mobility-" + "x" * 32},
        )

    assert raised.value.errors == (
        "[artifact_mobility].destination_origin invalid",
    )
    assert rejected_origin not in str(raised.value)


def test_enabled_mobility_bounds_an_oversized_decimal_port_without_echo(tmp_path, caplog):
    rejected_origin = "https://node-b.example:" + "9" * 5_000
    path = tmp_path / "sonder.toml"
    path.write_text(_enabled_toml(tmp_path, rejected_origin), encoding="utf-8")

    with pytest.raises(ConfigError) as raised:
        load_config(
            path,
            env={"SONDER_ARTIFACT_MOBILITY_PEER_KEY": "mobility-" + "x" * 32},
        )

    rendered_error = json.dumps({"errors": raised.value.errors})
    assert raised.value.errors == ("[artifact_mobility].destination_origin invalid",)
    assert rejected_origin not in str(raised.value)
    assert rejected_origin not in repr(raised.value)
    assert rejected_origin not in caplog.text
    assert rejected_origin not in rendered_error


@pytest.mark.parametrize(
    "origin",
    (
        "http://node-b.example:9443",
        "https://node-b.example",
        "https://node-b.example:9443/non-root",
        "https://node-b.example:9443?unexpected=true",
        "https://node-b.example:9443#fragment",
        "https://secret-user:leaked-value@node-b.example:9443",
        "https://node-b.example:0",
        "https://node\\evil.example:9443",
        "HTTPS://node-b.example:9443",
    ),
)
def test_enabled_mobility_rejects_every_noncanonical_origin_without_echo(tmp_path, origin):
    path = tmp_path / "sonder.toml"
    path.write_text(_enabled_toml(tmp_path, origin), encoding="utf-8")

    with pytest.raises(ConfigError) as raised:
        load_config(
            path,
            env={"SONDER_ARTIFACT_MOBILITY_PEER_KEY": "mobility-" + "x" * 32},
        )

    assert raised.value.errors == ("[artifact_mobility].destination_origin invalid",)
    assert origin not in str(raised.value)


def test_source_scope_is_stable_across_destination_configuration():
    source = ArtifactMobilitySourceConfig(
        enabled=True,
        store_dir="C:/private/export",
        principal_id="private-cluster",
        project_id="sonder",
        source_owner_id="node-a",
    )

    first = source_scope_id(source)
    changed_destination = replace(
        ArtifactMobilityConfig(),
        enabled=True,
        destination_label="node-b",
        destination_origin="https://node-b.example:9443",
    )

    assert first == source_scope_id(source)
    assert changed_destination.destination_label == "node-b"


def test_enabled_mobility_requires_pins_and_dedicated_peer_key(tmp_path):
    path = tmp_path / "sonder.toml"
    path.write_text(
        _enabled_toml(tmp_path, "https://node-b.example:9443")
        .replace(f'destination_tls_certificate_sha256 = {"a" * 64!r}', 'destination_tls_certificate_sha256 = ""')
        .replace(
            f'expected_recipient_attestation_sha256 = {"b" * 64!r}',
            'expected_recipient_attestation_sha256 = ""',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as raised:
        load_config(path, env={})

    assert "[artifact_mobility].destination_tls_certificate_sha256 invalid" in raised.value.errors
    assert "[artifact_mobility].expected_recipient_attestation_sha256 invalid" in raised.value.errors
    assert "[artifact_mobility].peer_key invalid" in raised.value.errors


def test_mobility_pins_must_be_exact_lowercase_hex(tmp_path):
    path = tmp_path / "sonder.toml"
    path.write_text(
        _enabled_toml(tmp_path, "https://node-b.example:9443").replace(
            "a" * 64, "A" * 64
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as raised:
        load_config(
            path,
            env={"SONDER_ARTIFACT_MOBILITY_PEER_KEY": "mobility-" + "x" * 32},
        )

    assert raised.value.errors == (
        "[artifact_mobility].destination_tls_certificate_sha256 invalid",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("max_object_bytes", 0),
        ("attempt_timeout_seconds", 31),
        ("attempt_lease_seconds", 30),
        ("receipt_ttl_seconds", 59),
        ("max_live_operations", 257),
    ),
)
def test_mobility_attempt_and_retention_limits_are_bounded(tmp_path, field, value):
    path = tmp_path / "sonder.toml"
    path.write_text(
        _enabled_toml(tmp_path, "https://node-b.example:9443")
        + f"{field} = {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as raised:
        load_config(
            path,
            env={"SONDER_ARTIFACT_MOBILITY_PEER_KEY": "mobility-" + "x" * 32},
        )

    assert raised.value.errors == (f"[artifact_mobility].{field} invalid",)


def test_url_like_peer_key_is_rejected_without_echo(tmp_path):
    path = tmp_path / "sonder.toml"
    path.write_text(
        _enabled_toml(tmp_path, "https://node-b.example:9443"),
        encoding="utf-8",
    )
    rejected_key = "https://leaked-peer-key/" + "x" * 40

    with pytest.raises(ConfigError) as raised:
        load_config(path, env={"SONDER_ARTIFACT_MOBILITY_PEER_KEY": rejected_key})

    assert "[artifact_mobility].peer_key invalid" in raised.value.errors
    assert rejected_key not in str(raised.value)
    assert rejected_key not in repr(raised.value)


def test_supplied_mobility_peer_key_is_validated_while_mobility_is_disabled():
    rejected_key = "https://leaked-peer-key/" + "x" * 40

    with pytest.raises(ConfigError) as raised:
        load_config(env={"SONDER_ARTIFACT_MOBILITY_PEER_KEY": rejected_key})

    assert raised.value.errors == ("[artifact_mobility].peer_key invalid",)
    assert rejected_key not in str(raised.value)
    assert rejected_key not in repr(raised.value)


def test_mobility_peer_key_rejects_process_control_characters_without_echo(tmp_path):
    path = tmp_path / "sonder.toml"
    path.write_text(
        _enabled_toml(tmp_path, "https://node-b.example:9443"),
        encoding="utf-8",
    )
    rejected_key = "mobility-" + "x" * 32 + "\t"

    with pytest.raises(ConfigError) as raised:
        load_config(path, env={"SONDER_ARTIFACT_MOBILITY_PEER_KEY": rejected_key})

    assert raised.value.errors == ("[artifact_mobility].peer_key invalid",)
    assert rejected_key not in str(raised.value)
    assert rejected_key not in repr(raised.value)


def test_malformed_mobility_peer_key_line_is_redacted(tmp_path):
    secrets = tmp_path / "secrets.env"
    rejected_fragment = "https://leaked-mobility-credential"
    secrets.write_text(
        f"{rejected_fragment} SONDER_ARTIFACT_MOBILITY_PEER_KEY",
        encoding="utf-8",
    )
    if os.name == "posix":
        secrets.chmod(0o600)

    with pytest.raises(ConfigError) as raised:
        load_config(secrets_path=secrets, env={})

    assert raised.value.errors == ("[artifact_mobility].peer_key malformed secrets input",)
    assert rejected_fragment not in str(raised.value)
    assert rejected_fragment not in repr(raised.value)


def test_malformed_mobility_peer_key_is_redacted_by_the_parser_too(tmp_path):
    secrets = tmp_path / "secrets.env"
    rejected_fragment = "https://leaked-mobility-credential"
    secrets.write_text(
        f"SONDER_ARTIFACT_MOBILITY_PEER_KEY {rejected_fragment}",
        encoding="utf-8",
    )

    with pytest.raises(config_environment.EnvironmentFileError) as raised:
        config_environment.parse_env_file(secrets)

    assert str(raised.value) == "[artifact_mobility].peer_key malformed secrets input"
    assert rejected_fragment not in repr(raised.value)


def test_mobility_parser_redacts_controls_and_continuation_like_lines(tmp_path):
    secrets = tmp_path / "secrets.env"
    rejected_fragment = "https://leaked-mobility-credential"
    secrets.write_text(
        "SONDER_ARTIFACT_MOBILITY_PEER_KEY=mobility-" + "x" * 32 + "\t",
        encoding="utf-8",
    )
    if os.name == "posix":
        secrets.chmod(0o600)

    with pytest.raises(config_environment.EnvironmentFileError) as raised:
        config_environment.parse_env_file(secrets)

    assert str(raised.value) == f"{secrets}:1: malformed secrets environment input"
    assert rejected_fragment not in repr(raised.value)

    secrets.write_text(
        "SONDER_ARTIFACT_MOBILITY_PEER_KEY=mobility-"
        + "x" * 32
        + "\n"
        + rejected_fragment,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as raised:
        load_config(secrets_path=secrets, env={})

    assert len(raised.value.errors) == 1
    assert raised.value.errors[0].startswith(f"{secrets}:")
    assert raised.value.errors[0].endswith(": expected KEY=VALUE")
    assert rejected_fragment not in str(raised.value)
    assert rejected_fragment not in repr(raised.value)


@pytest.mark.parametrize(
    "interruption",
    (
        "\n\n",
        "\n# harmless comment\n",
        "\x1e\x1e",
        "\x1e# harmless comment\x1e",
    ),
)
def test_mobility_parser_keeps_split_continuations_value_free(tmp_path, interruption):
    secrets = tmp_path / "secrets.env"
    rejected_fragment = "https://leaked-mobility-credential/continuation"
    secrets.write_text(
        "SONDER_ARTIFACT_MOBILITY_PEER_KEY=mobility-"
        + "x" * 32
        + interruption
        + rejected_fragment,
        encoding="utf-8",
    )

    with pytest.raises(config_environment.EnvironmentFileError) as raised:
        config_environment.parse_env_file(secrets)

    assert str(raised.value).startswith(f"{secrets}:")
    if "\x1e" in interruption:
        assert str(raised.value).endswith(": malformed secrets environment input")
    else:
        assert str(raised.value).endswith(": expected KEY=VALUE")
    assert rejected_fragment not in str(raised.value)
    assert "https://leaked-mobility" not in repr(raised.value)


def test_mobility_parser_failure_stays_out_of_errors_logs_and_serialization(tmp_path, caplog):
    secrets = tmp_path / "secrets.env"
    rejected_fragment = "https://leaked-mobility-credential/continuation"
    secrets.write_text(
        "SONDER_ARTIFACT_MOBILITY_PEER_KEY=mobility-"
        + "x" * 32
        + "\x1e# harmless comment\x1e"
        + rejected_fragment,
        encoding="utf-8",
    )
    if os.name == "posix":
        secrets.chmod(0o600)

    with pytest.raises(ConfigError) as raised:
        load_config(secrets_path=secrets, env={})

    rendered_error = json.dumps({"errors": raised.value.errors})
    assert len(raised.value.errors) == 1
    assert raised.value.errors[0].startswith(f"{secrets}:")
    assert raised.value.errors[0].endswith(": malformed secrets environment input")
    assert rejected_fragment not in str(raised.value)
    assert rejected_fragment not in repr(raised.value)
    assert rejected_fragment not in caplog.text
    assert rejected_fragment not in rendered_error


@pytest.mark.parametrize(
    "unrecognized_assignment",
    (
        "UNRECOGNIZED_RECORD=value",
        'UNRECOGNIZED_RECORD = "value"',
        "UNRECOGNIZED_RECORD=",
        "UNRECOGNIZED_RECORD=1",
        "UNRECOGNIZED_RECORD=quoted-value",
        "UNRECOGNIZED_RECORD=another=value",
    ),
)
def test_malformed_secrets_input_never_reflects_raw_content_after_unknown_assignment(
    tmp_path,
    caplog,
    unrecognized_assignment,
):
    secrets = tmp_path / "secrets.env"
    rejected_fragment = "https://leaked-mobility-credential/continuation"
    secrets.write_text(
        "SONDER_ARTIFACT_MOBILITY_PEER_KEY=mobility-"
        + "x" * 32
        + "\n"
        + unrecognized_assignment
        + "\n\n# harmless comment\n\x1e# control comment\x1e"
        + rejected_fragment,
        encoding="utf-8",
    )
    if os.name == "posix":
        secrets.chmod(0o600)

    with pytest.raises(config_environment.EnvironmentFileError) as parser_error:
        config_environment.parse_env_file(secrets)
    with pytest.raises(ConfigError) as config_error:
        load_config(secrets_path=secrets, env={})

    parser_message = str(parser_error.value)
    rendered_error = json.dumps({"errors": config_error.value.errors})
    assert parser_message.startswith(f"{secrets}:")
    assert parser_message.endswith(": malformed secrets environment input")
    assert config_error.value.errors == (parser_message,)
    for raw_input in (
        rejected_fragment,
        "https://leaked-mobility",
        unrecognized_assignment,
    ):
        assert raw_input not in parser_message
        assert raw_input not in repr(parser_error.value)
        assert raw_input not in str(config_error.value)
        assert raw_input not in repr(config_error.value)
        assert raw_input not in caplog.text
        assert raw_input not in rendered_error


def test_mobility_key_is_toml_forbidden_and_private_values_are_redacted(tmp_path):
    origin = "https://node-b.example:9443"
    path = tmp_path / "sonder.toml"
    path.write_text(
        _enabled_toml(tmp_path, origin)
        + "artifact_mobility_peer_key = \"toml-peer-key-must-not-be-used\"\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as raised:
        load_config(
            path,
            env={"SONDER_ARTIFACT_MOBILITY_PEER_KEY": "mobility-" + "x" * 32},
        )

    assert raised.value.errors == (
        "secret value 'artifact_mobility.artifact_mobility_peer_key' may not appear "
        "in TOML; use the secrets environment file",
    )
    assert "toml-peer-key-must-not-be-used" not in str(raised.value)

    path.write_text(_enabled_toml(tmp_path, origin), encoding="utf-8")
    peer_key = "mobility-" + "x" * 32
    config = load_config(path, env={"SONDER_ARTIFACT_MOBILITY_PEER_KEY": peer_key})
    rendered = str(config.as_redacted_dict())
    assert config.as_redacted_dict()["artifact_mobility"]["destination_origin"] == "[set]"
    assert config.as_redacted_dict()["artifact_mobility_source"]["store_dir"] == "[set]"
    for private_value in (
        origin,
        "a" * 64,
        "b" * 64,
        peer_key,
        str(tmp_path / "export-spool"),
    ):
        assert private_value not in rendered
        assert private_value not in repr(config)


@pytest.mark.parametrize(
    "other_secret_name",
    (
        "SONDER_API_KEY",
        "SONDER_AUTH_SECRET",
        "SONDER_ARTIFACT_TRANSFER_KEY",
        "SONDER_MEMORY_REPLICATION_KEY",
        "SONDER_MEMORY_REPLICATION_STATE_INTEGRITY_KEY",
    ),
)
def test_mobility_peer_key_must_be_distinct_from_existing_credentials(
    tmp_path, other_secret_name
):
    path = tmp_path / "sonder.toml"
    path.write_text(_enabled_toml(tmp_path, "https://node-b.example:9443"), encoding="utf-8")
    shared = "shared-private-key-" + "x" * 32

    with pytest.raises(ConfigError) as raised:
        load_config(
            path,
            env={
                "SONDER_ARTIFACT_MOBILITY_PEER_KEY": shared,
                other_secret_name: shared,
            },
        )

    assert raised.value.errors == ("[artifact_mobility].peer_key must be distinct",)
    assert shared not in str(raised.value)


def test_enabled_memory_and_mobility_reject_cross_feature_secret_reuse(tmp_path):
    path = tmp_path / "sonder.toml"
    path.write_text(
        _enabled_toml(tmp_path, "https://node-b.example:9443")
        + _enabled_memory_replication_toml(),
        encoding="utf-8",
    )
    shared = "shared-private-key-" + "x" * 40

    with pytest.raises(ConfigError) as raised:
        load_config(
            path,
            env={
                "SONDER_ARTIFACT_MOBILITY_PEER_KEY": shared,
                "SONDER_MEMORY_REPLICATION_KEY": shared,
                "SONDER_MEMORY_REPLICATION_STATE_INTEGRITY_KEY": (
                    "memory-state-" + "s" * 40
                ),
            },
        )

    assert raised.value.errors == (
        "memory replication dedicated key must be distinct from API, "
        "artifact-transfer, artifact-mobility, and auth secrets",
        "[artifact_mobility].peer_key must be distinct",
    )
    assert shared not in str(raised.value)


def test_source_store_rejects_configured_writable_root_overlap(tmp_path):
    source_store = tmp_path / "workspace"
    path = tmp_path / "sonder.toml"
    text = _enabled_toml(
        tmp_path,
        "https://node-b.example:9443",
        source_store=source_store,
    )
    text += "\n[state]\n" + f"workspace_roots = [{json.dumps(str(source_store))}]\n"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError) as raised:
        load_config(
            path,
            env={"SONDER_ARTIFACT_MOBILITY_PEER_KEY": "mobility-" + "x" * 32},
        )

    assert "[artifact_mobility_source].store_dir overlaps configured writable root" in raised.value.errors


def test_source_store_rejects_state_home_overlap(tmp_path):
    source_store = tmp_path / "state" / "export-spool"
    path = tmp_path / "sonder.toml"
    text = _enabled_toml(
        tmp_path,
        "https://node-b.example:9443",
        source_store=source_store,
    )
    text += "\n[state]\n" + f"home = {json.dumps(str(tmp_path / 'state'))}\n"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError) as raised:
        load_config(
            path,
            env={"SONDER_ARTIFACT_MOBILITY_PEER_KEY": "mobility-" + "x" * 32},
        )

    assert raised.value.errors == (
        "[artifact_mobility_source].store_dir overlaps configured writable root",
    )


def test_source_store_rejects_receiver_store_overlap(tmp_path):
    source_store = tmp_path / "shared-private"
    path = tmp_path / "sonder.toml"
    text = _enabled_toml(
        tmp_path,
        "https://node-b.example:9443",
        source_store=source_store,
    )
    text += (
        "\n[artifact_transfer]\n"
        "enabled = true\n"
        f"store_dir = {json.dumps(str(source_store))}\n"
        'principal_id = "private-cluster"\n'
        'project_id = "sonder"\n'
        'peer_node_id = "node-c"\n'
        'grant_id = "receiver-grant"\n'
        f"expires_at = {int(time.time()) + 3600}\n"
        "can_write = true\n"
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError) as raised:
        load_config(
            path,
            env={
                "SONDER_ARTIFACT_MOBILITY_PEER_KEY": "mobility-" + "x" * 32,
                "SONDER_ARTIFACT_TRANSFER_KEY": "receiver-" + "y" * 32,
            },
        )

    assert "[artifact_mobility_source].store_dir overlaps artifact transfer store" in raised.value.errors


def test_mobility_does_not_claim_a_remote_limit_from_local_receiver_config(tmp_path):
    path = tmp_path / "sonder.toml"
    text = _enabled_toml(tmp_path, "https://node-b.example:9443")
    text += (
        "\n[artifact_transfer]\n"
        "enabled = true\n"
        f"store_dir = {json.dumps(str(tmp_path / 'separate-receiver-store'))}\n"
        'principal_id = "private-cluster"\n'
        'project_id = "sonder"\n'
        'peer_node_id = "node-a"\n'
        'grant_id = "receiver-grant"\n'
        f"expires_at = {int(time.time()) + 3600}\n"
        "can_write = true\n"
        "max_object_bytes = 1\n"
    )
    path.write_text(text, encoding="utf-8")

    config = load_config(
        path,
        env={
            "SONDER_ARTIFACT_MOBILITY_PEER_KEY": "mobility-" + "x" * 32,
            "SONDER_ARTIFACT_TRANSFER_KEY": "receiver-" + "y" * 32,
        },
    )

    assert config.artifact_mobility.max_object_bytes > config.artifact_transfer.max_object_bytes


@pytest.mark.parametrize("invalid_identity", ("receiver\nidentity", 0, None))
def test_receiver_identity_is_optional_for_legacy_transfer_but_bounded_when_present(
    tmp_path,
    invalid_identity,
):
    from sonder_runtime.platform.artifact_transfer_config import (
        ArtifactTransferConfig,
        artifact_transfer_errors,
    )

    legacy = SonderConfig(
        state=StateConfig(home=str(tmp_path / "state")),
        secrets=Secrets(artifact_transfer_key="receiver-" + "x" * 32),
        artifact_transfer=ArtifactTransferConfig(
            enabled=True,
            store_dir=str(tmp_path / "receiver"),
            principal_id="private-cluster",
            project_id="sonder",
            peer_node_id="node-a",
            grant_id="receiver-grant",
            expires_at=int(time.time()) + 3600,
            can_write=True,
        ),
    )
    assert artifact_transfer_errors(legacy) == []

    invalid_identity = replace(
        legacy,
        artifact_transfer=replace(
            legacy.artifact_transfer,
            receiver_identity_id=invalid_identity,
        ),
    )
    assert "[artifact_transfer].receiver_identity_id invalid" in artifact_transfer_errors(
        invalid_identity
    )


def test_mobility_object_cap_may_not_exceed_the_local_source_cap(tmp_path):
    path = tmp_path / "sonder.toml"
    path.write_text(
        _enabled_toml(tmp_path, "https://node-b.example:9443").replace(
            'source_owner_id = "node-a"\n',
            'source_owner_id = "node-a"\nmax_object_bytes = 1\n',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as raised:
        load_config(
            path,
            env={"SONDER_ARTIFACT_MOBILITY_PEER_KEY": "mobility-" + "x" * 32},
        )

    assert raised.value.errors == ("[artifact_mobility].max_object_bytes invalid",)


def test_valid_remote_compute_configuration_does_not_enable_mobility(tmp_path):
    path = tmp_path / "sonder.toml"
    path.write_text(
        "[compute]\n"
        "allow_remote = true\n"
        "\n"
        "[[compute.nodes]]\n"
        'id = "node-b"\n'
        'origin = "https://node-b.example:9443"\n'
        'workloads = ["test"]\n'
        'capabilities = ["cpu"]\n',
        encoding="utf-8",
    )

    config = load_config(path, env={"SONDER_API_KEY": "admin-" + "x" * 32})

    assert config.compute.allow_remote is True
    assert config.artifact_mobility == ArtifactMobilityConfig()
    assert config.artifact_mobility_source == ArtifactMobilitySourceConfig()
