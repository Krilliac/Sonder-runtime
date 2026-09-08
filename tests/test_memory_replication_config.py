"""Fail-closed configuration tests for the uncomposed memory peer boundary."""
from __future__ import annotations

from dataclasses import replace

import pytest

from sonder_runtime.platform import config_environment
from sonder_runtime.platform.config import (
    ConfigError,
    Secrets,
    ServerConfig,
    SonderConfig,
    load_config,
)
from sonder_runtime.platform.memory_replication_config import (
    MemoryReplicationConfig,
    MemoryReplicationPeerConfig,
    memory_replication_errors,
)


def _key(char: str = "k") -> str:
    return "memory-replication-" + char * 40


def _state_key(char: str = "s") -> str:
    return "memory-replication-state-" + char * 40


def _toml(*, peer_scope: str = "repo-a", origin: str = "https://node-b.example:8443") -> str:
    return f'''[memory_replication]
enabled = true
local_node_id = "node-a"
project_scope = "repo-a"
receiver_enabled = true
accepted_source_ids = ["node-b"]
request_timeout_seconds = 5
max_request_bytes = 8192
max_response_bytes = 4096
max_batch_records = 16

[[memory_replication.peers]]
node_id = "node-b"
project_scope = "{peer_scope}"
origin = "{origin}"
'''


def _load(tmp_path, text: str, *, env: dict[str, str] | None = None):
    path = tmp_path / "sonder.toml"
    path.write_text(text, encoding="utf-8")
    effective_env = {
        "SONDER_MEMORY_REPLICATION_KEY": _key(),
        "SONDER_MEMORY_REPLICATION_STATE_INTEGRITY_KEY": _state_key(),
    }
    if env is not None:
        effective_env.update(env)
    return load_config(path, env=effective_env)


def _direct_enabled_config(
    *,
    secrets: Secrets | None = None,
    local_node_id: object = "node-a",
    project_scope: object = "repo-a",
    peer_node_id: object = "node-b",
    peer_scope: object = "repo-a",
    peer_origin: object = "https://node-b.example:8443",
    receiver_enabled: bool = False,
    accepted_source_ids: tuple[object, ...] = (),
    server: ServerConfig | None = None,
) -> SonderConfig:
    return SonderConfig(
        server=ServerConfig() if server is None else server,
        secrets=(
            Secrets(
                memory_replication_key=_key(),
                memory_replication_state_integrity_key=_state_key(),
            )
            if secrets is None
            else secrets
        ),
        memory_replication=MemoryReplicationConfig(
            enabled=True,
            local_node_id=local_node_id,
            project_scope=project_scope,
            receiver_enabled=receiver_enabled,
            accepted_source_ids=accepted_source_ids,
            peers=(MemoryReplicationPeerConfig(
                node_id=peer_node_id,
                project_scope=peer_scope,
                origin=peer_origin,
            ),),
        ),
    )


def test_memory_replication_defaults_are_disabled_and_empty():
    config = SonderConfig()
    assert config.memory_replication == MemoryReplicationConfig()
    assert config.secrets.memory_replication_key == ""
    assert memory_replication_errors(config) == []


def test_typed_peer_config_uses_dedicated_environment_secret_and_redacts(tmp_path):
    key = _key("z")
    config = _load(tmp_path, _toml(), env={"SONDER_MEMORY_REPLICATION_KEY": key})

    assert config.memory_replication == MemoryReplicationConfig(
        enabled=True,
        local_node_id="node-a",
        project_scope="repo-a",
        receiver_enabled=True,
        accepted_source_ids=("node-b",),
        peers=(MemoryReplicationPeerConfig(
            node_id="node-b",
            project_scope="repo-a",
            origin="https://node-b.example:8443",
        ),),
        request_timeout_seconds=5,
        max_request_bytes=8192,
        max_response_bytes=4096,
        max_batch_records=16,
    )
    assert config.secrets.memory_replication_key == key
    dumped = repr(config.as_redacted_dict())
    assert key not in dumped
    assert key not in repr(config)
    assert config.as_redacted_dict()["secrets"]["memory_replication_key"] == "[set]"
    assert config.as_redacted_dict()["memory_replication"]["peers"] == [
        {"node_id": "node-b", "project_scope": "repo-a", "origin": "[configured]"},
    ]


def test_dedicated_secret_loads_from_the_secrets_environment_file(tmp_path):
    path = tmp_path / "sonder.toml"
    path.write_text(_toml(), encoding="utf-8")
    secrets_path = tmp_path / "sonder.secrets.env"
    key = _key("s")
    state_key = _state_key("s")
    secrets_path.write_text(
        "SONDER_MEMORY_REPLICATION_KEY=" + key + "\n"
        "SONDER_MEMORY_REPLICATION_STATE_INTEGRITY_KEY=" + state_key + "\n",
        encoding="utf-8",
    )

    config = load_config(path, secrets_path=secrets_path, env={})

    assert config.secrets.memory_replication_key == key
    assert config.secrets.memory_replication_state_integrity_key == state_key
    assert key not in repr(config)
    assert config.as_redacted_dict()["secrets"]["memory_replication_key"] == "[set]"


def test_local_state_integrity_secret_is_environment_only_and_redacted(tmp_path):
    path = tmp_path / "sonder.toml"
    path.write_text(_toml(), encoding="utf-8")
    secrets_path = tmp_path / "sonder.secrets.env"
    peer_key = _key("p")
    state_key = _state_key("l")
    secrets_path.write_text(
        "SONDER_MEMORY_REPLICATION_KEY=" + peer_key + "\n"
        "SONDER_MEMORY_REPLICATION_STATE_INTEGRITY_KEY=" + state_key + "\n",
        encoding="utf-8",
    )

    config = load_config(path, secrets_path=secrets_path, env={})

    assert config.secrets.memory_replication_key == peer_key
    assert config.secrets.memory_replication_state_integrity_key == state_key
    rendered = repr(config.as_redacted_dict()) + repr(config)
    assert peer_key not in rendered
    assert state_key not in rendered
    assert (
        config.as_redacted_dict()["secrets"]
        ["memory_replication_state_integrity_key"]
        == "[set]"
    )


def test_state_integrity_secret_is_rejected_from_toml(tmp_path):
    state_key = _state_key("t")
    text = _toml().replace(
        "\n[[memory_replication.peers]]",
        "\nmemory_replication_state_integrity_key = \"" + state_key
        + "\"\n\n[[memory_replication.peers]]",
    )

    with pytest.raises(ConfigError) as error:
        _load(tmp_path, text, env={
            "SONDER_MEMORY_REPLICATION_KEY": _key(),
            "SONDER_MEMORY_REPLICATION_STATE_INTEGRITY_KEY": _state_key(),
        })

    assert "secrets environment file" in str(error.value)
    assert state_key not in str(error.value)


def test_enabled_config_requires_a_distinct_local_state_integrity_secret(tmp_path):
    shared = _key("x")

    with pytest.raises(ConfigError) as error:
        _load(tmp_path, _toml(), env={
            "SONDER_MEMORY_REPLICATION_KEY": shared,
            "SONDER_MEMORY_REPLICATION_STATE_INTEGRITY_KEY": shared,
        })

    assert "state-integrity" in str(error.value)
    assert shared not in str(error.value)


@pytest.mark.parametrize(
    "field",
    (
        "memory_replication_key",
        "api_key",
        "artifact_transfer_key",
        "artifact_mobility_peer_key",
        "auth_secret",
    ),
)
def test_direct_typed_config_rejects_state_key_reuse_with_every_secret_boundary(field):
    state_key = _state_key("r")
    values = {
        "memory_replication_key": _key(),
        "memory_replication_state_integrity_key": state_key,
        "api_key": "api-" + "a" * 40,
        "artifact_transfer_key": "artifact-" + "b" * 40,
        "artifact_mobility_peer_key": "mobility-" + "m" * 40,
        "auth_secret": "auth-" + "c" * 40,
    }
    values[field] = state_key

    errors = memory_replication_errors(
        _direct_enabled_config(secrets=Secrets(**values))
    )

    assert errors == [
        "memory replication local state-integrity secret must be distinct "
        "from replication peer, API, artifact-transfer, artifact-mobility, "
        "and auth secrets"
    ]
    assert state_key not in repr(errors)


@pytest.mark.parametrize(
    "value",
    (None, 0, [], b"not-a-text-state-secret", {"secret": "not-a-text-state-secret"}),
)
def test_injected_state_integrity_key_requires_a_string_before_normalization(value):
    with pytest.raises(ConfigError) as error:
        load_config(env={"SONDER_MEMORY_REPLICATION_STATE_INTEGRITY_KEY": value})

    assert error.value.errors == (
        "SONDER_MEMORY_REPLICATION_STATE_INTEGRITY_KEY must be a string",
    )
    assert "not-a-text-state-secret" not in str(error.value)
    assert "not-a-text-state-secret" not in repr(error.value)


def test_topology_never_falls_back_to_injected_environment_values(tmp_path):
    config = _load(
        tmp_path,
        _toml(),
        env={
            "SONDER_MEMORY_REPLICATION_ENABLED": "false",
            "SONDER_MEMORY_REPLICATION_LOCAL_NODE_ID": "attacker",
            "SONDER_MEMORY_REPLICATION_PROJECT_SCOPE": "repo-other",
            "SONDER_MEMORY_REPLICATION_PEERS": "http://attacker.invalid:1",
        },
    )
    assert config.memory_replication.enabled is True
    assert config.memory_replication.local_node_id == "node-a"
    assert config.memory_replication.project_scope == "repo-a"
    assert config.memory_replication.peers[0].origin == "https://node-b.example:8443"
    assert not load_config(
        env={"SONDER_MEMORY_REPLICATION_ENABLED": "true"},
    ).memory_replication.enabled


def test_command_line_overrides_cannot_select_a_replication_scope(tmp_path):
    path = tmp_path / "sonder.toml"
    path.write_text(_toml(), encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        load_config(
            path,
            env={"SONDER_MEMORY_REPLICATION_KEY": _key()},
            overrides={"memory_replication.project_scope": "repo-other"},
        )

    assert "invalid override section 'memory_replication'" in str(error.value)


def test_toml_secret_is_rejected_without_reflecting_its_value(tmp_path):
    secret = "https://secret-value.example/never-show"
    text = _toml().replace(
        "\n[[memory_replication.peers]]",
        "\nmemory_replication_key = \"" + secret + "\"\n\n[[memory_replication.peers]]",
    )
    with pytest.raises(ConfigError) as error:
        _load(tmp_path, text, env={"SONDER_MEMORY_REPLICATION_KEY": ""})
    rendered = str(error.value)
    assert "secrets environment file" in rendered
    assert secret not in rendered


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"SONDER_MEMORY_REPLICATION_KEY": ""}, "dedicated"),
        ({"SONDER_MEMORY_REPLICATION_KEY": "a" * 12}, "dedicated"),
        ({"SONDER_MEMORY_REPLICATION_KEY": _key(), "SONDER_API_KEY": _key()}, "distinct"),
        ({
            "SONDER_MEMORY_REPLICATION_KEY": _key(),
            "SONDER_ARTIFACT_TRANSFER_KEY": _key(),
        }, "distinct"),
        ({
            "SONDER_MEMORY_REPLICATION_KEY": _key(),
            "SONDER_ARTIFACT_MOBILITY_PEER_KEY": _key(),
        }, "distinct"),
        ({"SONDER_MEMORY_REPLICATION_KEY": _key(), "SONDER_AUTH_SECRET": _key()}, "distinct"),
    ],
)
def test_enabled_config_requires_distinct_dedicated_secret(tmp_path, env, expected):
    with pytest.raises(ConfigError) as error:
        _load(tmp_path, _toml(), env=env)
    rendered = str(error.value)
    assert expected in rendered
    for value in env.values():
        if value:
            assert value not in rendered


@pytest.mark.parametrize(
    "origin",
    [
        "http://node-b.example:8443",
        "HTTPS://node-b.example:8443",
        "https://node-b.example",
        "https://node-b.example:08443",
        "https://node-b.example:0",
        "https://node-b.example:65536",
        "https://node-b.example:8443/path",
        "https://user:password@node-b.example:8443",
        "https://*.example:8443",
        "https://NODE-B.example:8443",
    ],
)
def test_outbound_peer_origin_is_exact_https_and_non_disclosing(tmp_path, origin):
    with pytest.raises(ConfigError) as error:
        _load(tmp_path, _toml(origin=origin))
    rendered = str(error.value)
    assert "origin" in rendered
    assert origin not in rendered


def test_config_rejects_cross_scope_and_duplicate_or_wildcard_identities(tmp_path):
    with pytest.raises(ConfigError) as error:
        _load(tmp_path, _toml(peer_scope="repo-b"))
    assert "exactly match" in str(error.value)

    duplicate_sources = _toml().replace(
        'accepted_source_ids = ["node-b"]',
        'accepted_source_ids = ["node-b", "node-b"]',
    )
    with pytest.raises(ConfigError) as error:
        _load(tmp_path, duplicate_sources)
    assert "duplicates" in str(error.value)

    wildcard_source = _toml().replace(
        'accepted_source_ids = ["node-b"]',
        'accepted_source_ids = ["*"]',
    )
    with pytest.raises(ConfigError) as error:
        _load(tmp_path, wildcard_source)
    assert "bounded stable identities" in str(error.value)


def test_receiver_requires_secure_listener_and_fixed_peer_admission(tmp_path):
    insecure = (
        '[server]\n'
        'host = "0.0.0.0"\n'
        'tls_terminated_by_proxy = false\n\n'
    ) + _toml()
    with pytest.raises(ConfigError) as error:
        _load(tmp_path, insecure)
    assert "tls_terminated_by_proxy" in str(error.value)

    unconfigured_source = _toml().replace(
        'accepted_source_ids = ["node-b"]',
        'accepted_source_ids = ["node-c"]',
    )
    with pytest.raises(ConfigError) as error:
        _load(tmp_path, unconfigured_source)
    assert "fixed configured peer" in str(error.value)


def test_unknown_proxy_or_caller_scope_setting_is_not_a_configuration_escape(tmp_path):
    attempted_proxy = _toml().replace(
        "\n[[memory_replication.peers]]",
        '\nproxy = "https://proxy.invalid:8443"\n\n[[memory_replication.peers]]',
    )
    with pytest.raises(ConfigError) as error:
        _load(tmp_path, attempted_proxy)
    rendered = str(error.value)
    assert "unknown key [memory_replication].proxy" in rendered
    assert "https://proxy.invalid:8443" not in rendered

    attempted_scope = _toml().replace(
        "\n[[memory_replication.peers]]",
        '\nrequest_project_scope = "repo-other"\n\n[[memory_replication.peers]]',
    )
    with pytest.raises(ConfigError) as error:
        _load(tmp_path, attempted_scope)
    assert "unknown key [memory_replication].request_project_scope" in str(error.value)


def test_direct_typed_config_cannot_enable_receiver_without_global_enablement():
    config = replace(
        SonderConfig(),
        memory_replication=MemoryReplicationConfig(receiver_enabled=True),
    )
    assert any("requires enabled" in message for message in memory_replication_errors(config))


def test_direct_typed_config_rejects_nonstring_sources_without_raising():
    config = SonderConfig(
        secrets=Secrets(
            memory_replication_key=_key(),
            memory_replication_state_integrity_key=_state_key(),
        ),
        memory_replication=MemoryReplicationConfig(
            enabled=True,
            local_node_id="node-a",
            project_scope="repo-a",
            receiver_enabled=True,
            accepted_source_ids=(["node-b"],),
            peers=(MemoryReplicationPeerConfig(
                node_id="node-b",
                project_scope="repo-a",
                origin="https://node-b.example:8443",
            ),),
        ),
    )

    errors = memory_replication_errors(config)

    assert any("accepted_source_ids must contain" in message for message in errors)


def test_direct_typed_config_rejects_dedicated_secret_str_subclass_before_equality():
    class EvasiveSecret(str):
        def __eq__(self, other):
            return False

    key = _key("e")
    config = SonderConfig(
        secrets=Secrets(
            memory_replication_key=EvasiveSecret(key),
            memory_replication_state_integrity_key=_state_key(),
            auth_secret=key,
        ),
        memory_replication=MemoryReplicationConfig(
            enabled=True,
            local_node_id="node-a",
            project_scope="repo-a",
            peers=(MemoryReplicationPeerConfig(
                node_id="node-b",
                project_scope="repo-a",
                origin="https://node-b.example:8443",
            ),),
        ),
    )

    errors = memory_replication_errors(config)

    assert errors == [
        "memory replication requires a dedicated 32..512 character secret",
    ]


@pytest.mark.parametrize(
    "field",
    ("api_key", "artifact_transfer_key", "auth_secret"),
)
def test_direct_typed_config_rejects_subclassed_comparison_secret_before_equality(
    field,
):
    class UnequalSecret(str):
        def __eq__(self, other):
            return False

    key = _key("c")
    config = _direct_enabled_config(
        secrets=Secrets(
            memory_replication_key=key,
            memory_replication_state_integrity_key=_state_key(),
            **{field: UnequalSecret(key)},
        ),
    )

    errors = memory_replication_errors(config)

    assert errors == [
        "memory replication secret separation requires exact builtin strings",
    ]
    assert key not in repr(errors)


def test_direct_typed_config_rejects_local_identity_subclass_before_peer_membership():
    class HidingIdentity(str):
        def __eq__(self, other):
            return False

    config = _direct_enabled_config(
        local_node_id=HidingIdentity("node-a"),
        peer_node_id="node-a",
    )

    errors = memory_replication_errors(config)

    assert errors == [
        "[memory_replication].local_node_id must be a bounded stable identity",
    ]


def test_direct_typed_config_rejects_scope_subclass_before_peer_scope_comparison():
    class HidingScope(str):
        def __ne__(self, other):
            return False

    config = _direct_enabled_config(
        project_scope=HidingScope("repo-other"),
        peer_scope="repo-a",
    )

    errors = memory_replication_errors(config)

    assert errors == [
        "[memory_replication].project_scope must be an exact bounded scope",
    ]


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    (
        (
            "peer_node_id",
            "node-b",
            "[memory_replication].peers[0].node_id must be a bounded stable identity",
        ),
        (
            "peer_scope",
            "repo-a",
            "[memory_replication].peers[0].project_scope must be an exact bounded scope",
        ),
    ),
)
def test_direct_typed_config_rejects_peer_string_subclasses(field, value, expected):
    class PeerString(str):
        pass

    config = _direct_enabled_config(**{field: PeerString(value)})

    assert memory_replication_errors(config) == [expected]


def test_direct_typed_config_rejects_host_subclass_that_claims_loopback():
    class PretendLoopback(str):
        def __eq__(self, other):
            return other == "localhost"

    config = _direct_enabled_config(
        receiver_enabled=True,
        accepted_source_ids=("node-b",),
        server=ServerConfig(
            host=PretendLoopback("0.0.0.0"),
            tls_terminated_by_proxy=False,
        ),
    )

    errors = memory_replication_errors(config)

    assert errors == [
        "[memory_replication].receiver_enabled requires a loopback listener or declared TLS proxy",
    ]


@pytest.mark.parametrize(
    "value",
    (None, 0, [], b"not-a-text-secret", {"secret": "not-a-text-secret"}),
)
def test_injected_replication_key_requires_a_string_before_normalization(value):
    with pytest.raises(ConfigError) as error:
        load_config(env={"SONDER_MEMORY_REPLICATION_KEY": value})

    assert error.value.errors == ("SONDER_MEMORY_REPLICATION_KEY must be a string",)
    assert "not-a-text-secret" not in str(error.value)
    assert "not-a-text-secret" not in repr(error.value)


@pytest.mark.parametrize("field", ("local_node_id", "project_scope"))
@pytest.mark.parametrize("value", (None, 0, False, [], {}))
def test_disabled_typed_config_rejects_falsey_nonstring_identity_or_scope(field, value):
    config = replace(
        SonderConfig(),
        memory_replication=replace(MemoryReplicationConfig(), **{field: value}),
    )

    errors = memory_replication_errors(config)

    expected = f"[memory_replication].{field} must be " + (
        "a bounded stable identity" if field == "local_node_id"
        else "an exact bounded scope"
    )
    assert errors == [expected]


@pytest.mark.parametrize(
    "secret_name",
    (
        "SONDER_MEMORY_REPLICATION_KEY",
        "SONDER_MEMORY_REPLICATION_STATE_INTEGRITY_KEY",
        "SONDER_ARTIFACT_TRANSFER_KEY",
        "SONDER_AUTH_SECRET",
    ),
)
def test_secrets_parser_rejects_control_characters_without_echo(tmp_path, secret_name):
    secrets = tmp_path / "secrets.env"
    fragment = "https://private-secret.example/control"
    secrets.write_text(f"{secret_name}={fragment}\t", encoding="utf-8")

    with pytest.raises(config_environment.EnvironmentFileError) as error:
        config_environment.parse_env_file(secrets)

    assert "malformed secrets environment input" in str(error.value)
    assert fragment not in str(error.value)
    assert fragment not in repr(error.value)


def test_malformed_secrets_file_after_blank_and_comment_never_echoes_input(tmp_path):
    secrets = tmp_path / "secrets.env"
    fragment = "https://private-secret.example/continuation"
    secrets.write_text(
        "SONDER_MEMORY_REPLICATION_KEY=" + _key() + "\n# ordinary comment\n\n" + fragment,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_config(secrets_path=secrets, env={})

    assert "expected KEY=VALUE" in str(error.value)
    assert "got" not in str(error.value)
    assert fragment not in str(error.value)
    assert fragment not in repr(error.value)


@pytest.mark.parametrize("separator", ("\r", "\u0085", "\u2028", "\u2029"))
def test_secrets_parser_rejects_noncanonical_line_separator_without_echo(
    tmp_path, separator
):
    secrets = tmp_path / "secrets.env"
    fragment = "https://private-secret.example/line-separator"
    secrets.write_text(
        "SONDER_MEMORY_REPLICATION_KEY=" + _key() + separator + fragment,
        encoding="utf-8",
    )

    with pytest.raises(config_environment.EnvironmentFileError) as error:
        config_environment.parse_env_file(secrets)

    assert "malformed secrets environment input" in str(error.value)
    assert fragment not in str(error.value)
    assert fragment not in repr(error.value)


def test_secrets_parser_rejects_invalid_utf8_without_echo(tmp_path):
    secrets = tmp_path / "secrets.env"
    fragment = "https://private-secret.example/invalid-utf8"
    secrets.write_bytes(
        b"SONDER_MEMORY_REPLICATION_KEY=" + fragment.encode("ascii") + b"\xff"
    )

    with pytest.raises(config_environment.EnvironmentFileError) as error:
        config_environment.parse_env_file(secrets)

    assert "malformed secrets environment input" in str(error.value)
    assert fragment not in str(error.value)
    assert fragment not in repr(error.value)
