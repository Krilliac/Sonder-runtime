"""SPEC-2 WP2: typed, deterministic, fail-closed configuration."""
from __future__ import annotations

import os

import pytest

import sonder_config
from sonder_config import ConfigError, load_config
from sonder_runtime.platform import config as platform_config
from sonder_runtime.platform.config import (
    BUILTIN_DEV_AUTH_SECRET,
    Secrets,
    ServerConfig,
    SonderConfig,
)

pytestmark = pytest.mark.unit

_CLEAN_ENV: dict[str, str] = {}


def _membership_toml(tmp_path):
    return f'''[ollama]
allow_remote = true
[membership]
mode = "external"
cluster_id = "cluster"
issuer_id = "issuer"
protocol_version = 1
source_origin = "https://registry.example:443"
source_tls_server_name = "registry.example"
source_allowed_cidrs = ["10.77.0.0/24"]
trust_anchor_file = "{tmp_path.as_posix()}/private-ca.pem"
signature_public_key_file = "{tmp_path.as_posix()}/membership-signer.pem"
refresh_interval_seconds = 30
snapshot_max_advertisements = 4096
snapshot_max_bytes = 1048576
local_fallback = false
[[membership.member_policies]]
member_id = "worker"
origin = "https://worker.example:11434"
tls_server_name = "worker.example"
allowed_cidrs = ["10.77.0.0/24"]
'''


def test_external_membership_is_explicit_and_secrets_are_redacted(tmp_path):
    assert load_config(env={}).membership.mode == "static"
    path = tmp_path / "membership.toml"
    path.write_text(_membership_toml(tmp_path), encoding="utf-8")
    with pytest.raises(ConfigError): load_config(path, env={})
    config = load_config(path, env={"SONDER_MEMBERSHIP_CLIENT_CERT_FILE": str(tmp_path / "private-client.pem"),
                                  "SONDER_MEMBERSHIP_CLIENT_KEY_FILE": str(tmp_path / "private-key.pem")})
    assert config.membership.member_policies[0].member_id == "worker"
    rendered = str(config.as_redacted_dict())
    assert "private-client.pem" not in rendered and "private-key.pem" not in rendered
    assert "registry.example" not in rendered and "worker.example" not in rendered
    for rendered in (repr(config.membership), repr(config.membership.member_policies[0]), repr(config)):
        for private in ("registry.example", "worker.example", "private-ca.pem", "membership-signer.pem",
                        "private-client.pem", "private-key.pem", "10.77.0.0/24"):
            assert private not in rendered


@pytest.mark.parametrize("old,new", [('mode = "external"', 'mode = "discovery"'),
    ('tls_server_name = "worker.example"', 'tls_server_name = "*.example"'),
    ('tls_server_name = "worker.example"', 'tls_server_name = ".example"'),
    ('origin = "https://worker.example:11434"', 'origin = "http://worker.example:11434"'),
    ('allowed_cidrs = ["10.77.0.0/24"]', 'allowed_cidrs = ["invalid"]'),
    ('snapshot_max_advertisements = 4096', 'snapshot_max_advertisements = 4097'),
    ('snapshot_max_bytes = 1048576', 'snapshot_max_bytes = 1048577'),
    ('protocol_version = 1', 'protocol_version = 2'),
    ('local_fallback = false', ''), ('trust_anchor_file = "private-ca.pem"', ''),
    ('allow_remote = true', 'allow_remote = false')])
def test_external_membership_rejects_unfixed_or_unbounded_policy(tmp_path, old, new):
    path = tmp_path / "membership.toml"
    old = old.replace('"private-ca.pem"', f'"{tmp_path.as_posix()}/private-ca.pem"')
    path.write_text(_membership_toml(tmp_path).replace(old, new), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, env={"SONDER_MEMBERSHIP_CLIENT_CERT_FILE": str(tmp_path / "client.pem"),
                               "SONDER_MEMBERSHIP_CLIENT_KEY_FILE": str(tmp_path / "key.pem")})


def test_external_member_policy_parser_has_a_hard_4096_entry_limit(tmp_path):
    path = tmp_path / "membership.toml"
    header, table = _membership_toml(tmp_path).split("[[membership.member_policies]]", 1)
    tables = ["[[membership.member_policies]]" + table.replace('"worker"', f'"worker{i}"').replace("worker.example", f"worker{i}.example")
              for i in range(4096)]
    env = {"SONDER_MEMBERSHIP_CLIENT_CERT_FILE":str(tmp_path / "client.pem"), "SONDER_MEMBERSHIP_CLIENT_KEY_FILE":str(tmp_path / "key.pem")}
    path.write_text(header + "".join(tables), encoding="utf-8")
    assert len(load_config(path, env=env).membership.member_policies) == 4096
    path.write_text(header + "".join(tables) + tables[0], encoding="utf-8")
    with pytest.raises(ConfigError): load_config(path, env=env)


@pytest.mark.parametrize("change", ["missing", "duplicate_id", "duplicate_origin", "credential"])
def test_external_policy_and_credential_input_fails_privately(tmp_path, change):
    header, table = _membership_toml(tmp_path).split("[[membership.member_policies]]", 1)
    if change == "missing": text = header
    elif change == "duplicate_id": text = _membership_toml(tmp_path) + "[[membership.member_policies]]" + table.replace("worker.example", "other.example")
    elif change == "duplicate_origin": text = _membership_toml(tmp_path) + "[[membership.member_policies]]" + table.replace('"worker"', '"other"')
    else: text = header + 'membership_client_key_file = "secret-inline-credential"\n[[membership.member_policies]]' + table
    path = tmp_path / "membership.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_config(path, env={"SONDER_MEMBERSHIP_CLIENT_CERT_FILE":str(tmp_path / "client.pem"), "SONDER_MEMBERSHIP_CLIENT_KEY_FILE":str(tmp_path / "key.pem")})
    for private in ("worker.example", "other.example", "secret-inline-credential", "private-ca.pem"):
        assert private not in str(caught.value)


@pytest.mark.parametrize("remote_primary", [False, True])
def test_trusted_cidr_does_not_allow_remote_http(tmp_path, remote_primary):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        '[ollama]\nallow_remote = true\ntrusted_origins = ["10.77.0.0/24"]\n'
        + ('url = "http://10.77.0.2:11434"\n' if remote_primary
           else 'workers = ["http://10.77.0.2:11434"]\n'), encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="https"):
        load_config(toml, env={})


def _strong_key() -> str:
    return "k" * sonder_config.MIN_API_KEY_LENGTH


def test_defaults_are_loopback_and_closed():
    config = load_config(env=_CLEAN_ENV)
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 11435
    assert config.profile == "workstation-local"
    assert config.features.cloud is False
    assert config.features.web is False
    assert config.ollama.allow_remote is False
    assert config.server.tls_terminated_by_proxy is False


def test_toml_profile_loads(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """
schema_version = 1
profile = "workstation-local"

[server]
port = 12345

[capacity]
queue_depth = 8
""",
        encoding="utf-8",
    )
    config = load_config(toml, env=_CLEAN_ENV)
    assert config.server.port == 12345
    assert config.capacity.queue_depth == 8
    assert str(toml) in config.sources


def test_non_loopback_without_tls_proxy_fails(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """
[server]
host = "0.0.0.0"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(toml, env={"SONDER_API_KEY": _strong_key()})
    assert any("tls_terminated_by_proxy" in e for e in excinfo.value.errors)


def test_non_loopback_without_strong_key_fails(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """
[server]
host = "0.0.0.0"
tls_terminated_by_proxy = true
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(toml, env={"SONDER_API_KEY": "short"})
    assert any("SONDER_API_KEY" in e for e in excinfo.value.errors)


def test_non_loopback_with_proxy_and_strong_key_passes(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """
[server]
host = "0.0.0.0"
tls_terminated_by_proxy = true
""",
        encoding="utf-8",
    )
    config = load_config(toml, env={"SONDER_API_KEY": _strong_key()})
    assert config.server.host == "0.0.0.0"


def test_direct_typed_host_subclass_cannot_claim_loopback():
    class PretendLoopback(str):
        def __eq__(self, other):
            return other == "localhost"

    errors: list[str] = []
    platform_config._validate(
        SonderConfig(server=ServerConfig(host=PretendLoopback("0.0.0.0"))),
        errors,
    )

    assert errors == ["[server].host must be an exact builtin string"]


def test_direct_typed_nonloopback_key_length_requires_builtin_string():
    class LongKey(str):
        def __len__(self):
            return sonder_config.MIN_API_KEY_LENGTH

    errors: list[str] = []
    platform_config._validate(
        SonderConfig(
            server=ServerConfig(
                host="0.0.0.0",
                tls_terminated_by_proxy=True,
            ),
            secrets=Secrets(api_key=LongKey("short")),
        ),
        errors,
    )

    assert errors == [
        "non-loopback binding requires SONDER_API_KEY of at least "
        f"{sonder_config.MIN_API_KEY_LENGTH} characters in the secrets file",
    ]


def test_direct_typed_builtin_auth_secret_subclass_cannot_hide_development_key():
    class UnequalSecret(str):
        def __eq__(self, other):
            return False

    errors: list[str] = []
    platform_config._validate(
        SonderConfig(
            server=ServerConfig(auth_mode="account"),
            secrets=Secrets(auth_secret=UnequalSecret(BUILTIN_DEV_AUTH_SECRET)),
        ),
        errors,
    )

    assert errors == [
        "[server].auth_secret must be an exact builtin string for "
        "account-bearing authentication",
    ]


def test_direct_typed_builtin_bind_values_remain_valid():
    errors: list[str] = []
    platform_config._validate(
        SonderConfig(
            server=ServerConfig(host="127.0.0.1", auth_mode="account"),
            secrets=Secrets(auth_secret="private-account-secret"),
        ),
        errors,
    )

    assert errors == []


def test_server_private_profile_requires_strong_key(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text('profile = "server-private"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(toml, env=_CLEAN_ENV)
    assert any("server-private" in e for e in excinfo.value.errors)
    config = load_config(toml, env={"SONDER_API_KEY": _strong_key()})
    assert config.profile == "server-private"


def test_secrets_in_toml_rejected(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """
[server]
api_key = "super-secret-value-here"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(toml, env=_CLEAN_ENV)
    assert any("secrets environment file" in e for e in excinfo.value.errors)


def test_all_errors_reported_together(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """
schema_version = 99
profile = "public-saas"

[server]
port = 999999
auth_mode = "none"

[observability]
log_level = "SHOUT"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(toml, env=_CLEAN_ENV)
    errors = "\n".join(excinfo.value.errors)
    assert "schema_version" in errors
    assert "profile" in errors
    assert "port" in errors
    assert "auth_mode" in errors
    assert "log_level" in errors
    assert len(excinfo.value.errors) >= 5


def test_unknown_keys_rejected(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """
[server]
prot = 1234

[surver]
port = 1234
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(toml, env=_CLEAN_ENV)
    errors = "\n".join(excinfo.value.errors)
    assert "[server].prot" in errors
    assert "surver" in errors


def test_env_compatibility_and_precedence(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text("[server]\nport = 12000\n", encoding="utf-8")
    config = load_config(
        toml,
        env={"SONDER_PORT": "13000", "SONDER_ALLOW_CLOUD": "1"},
    )
    assert config.server.port == 13000  # env beats TOML
    assert config.features.cloud is True
    config = load_config(
        toml,
        env={"SONDER_PORT": "13000"},
        overrides={"server.port": "14000"},
    )
    assert config.server.port == 14000  # CLI beats env


def test_historical_state_home_alias_is_supported_and_canonical_home_wins(tmp_path):
    historical = tmp_path / "historical"
    canonical = tmp_path / "canonical"

    config = load_config(
        env={"SONDER_STATE_HOME": str(historical)},
    )
    assert config.state.home == str(historical)

    config = load_config(
        env={
            "SONDER_HOME": str(canonical),
            "SONDER_STATE_HOME": str(historical),
        },
    )
    assert config.state.home == str(canonical)


def test_http_and_reasoning_options_are_typed_toml_settings(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """
[server]
max_concurrent_requests = 7
request_timeout_seconds = 91
stream_idle_timeout_seconds = 19
cors_origins = ["https://console.example"]
require_account = true
allow_registration = true
reasoning_audience = "all"
session_state_limit = 48
session_state_owner_limit = 12
train_max_n = 77

[features]
expose_reasoning = true
allow_private_cot = true
location_consent = true
""",
        encoding="utf-8",
    )
    config = load_config(toml, env={"SONDER_AUTH_SECRET": "private-test-secret"})
    assert config.server.max_concurrent_requests == 7
    assert config.server.cors_origins == ("https://console.example",)
    assert config.server.require_account is True
    assert config.server.reasoning_audience == "all"
    assert config.server.session_state_owner_limit == 12
    assert config.features.allow_private_cot is True
    assert config.features.location_consent is True


@pytest.mark.parametrize(
    "setting, value",
    [
        ("session_state_limit", "1"),
        ("session_state_limit", "1025"),
        ("session_state_owner_limit", "0"),
    ],
)
def test_session_state_limits_match_http_adapter_bounds(tmp_path, setting, value):
    toml = tmp_path / "sonder.toml"
    toml.write_text("[server]\n%s = %s\n" % (setting, value), encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(toml, env=_CLEAN_ENV)
    assert "session_state" in "\n".join(excinfo.value.errors)


def test_secrets_file_loaded_and_permission_checked(tmp_path):
    secrets = tmp_path / "sonder.env"
    secrets.write_text(f"SONDER_API_KEY={_strong_key()}\n", encoding="utf-8")
    os.chmod(secrets, 0o600)
    config = load_config(secrets_path=secrets, env=_CLEAN_ENV)
    assert config.secrets.api_key == _strong_key()

    if os.name == "posix":
        os.chmod(secrets, 0o644)
        with pytest.raises(ConfigError) as excinfo:
            load_config(secrets_path=secrets, env=_CLEAN_ENV)
        assert any("group/world" in e for e in excinfo.value.errors)


def test_process_env_beats_secrets_file(tmp_path):
    secrets = tmp_path / "sonder.env"
    secrets.write_text("SONDER_API_KEY=" + "a" * 32 + "\n", encoding="utf-8")
    os.chmod(secrets, 0o600)
    config = load_config(
        secrets_path=secrets, env={"SONDER_API_KEY": "b" * 32}
    )
    assert config.secrets.api_key == "b" * 32


def test_remote_ollama_requires_consent():
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={"OLLAMA_HOST": "192.168.1.50:11434"})
    assert any("remote-Ollama consent" in e for e in excinfo.value.errors)
    config = load_config(
        env={
            "OLLAMA_HOST": "https://192.168.1.50:11434",
            "SONDER_ALLOW_REMOTE_OLLAMA": "1",
        }
    )
    assert config.ollama.allow_remote is True


def test_remote_ollama_requires_https_even_with_consent():
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            env={
                "OLLAMA_HOST": "http://192.168.1.50:11434",
                "SONDER_ALLOW_REMOTE_OLLAMA": "1",
            }
        )
    assert any("must use https" in error for error in excinfo.value.errors)


def test_remote_ollama_workers_require_consent_and_parse_lists():
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={"SONDER_OLLAMA_WORKERS": "192.168.1.20:11434"})
    assert any("workers remote entries require" in e for e in excinfo.value.errors)
    config = load_config(
        env={
            "SONDER_ALLOW_REMOTE_OLLAMA": "1",
            "SONDER_OLLAMA_WORKERS": (
                "https://192.168.1.20:11434;https://192.168.1.21:11434"
            ),
        }
    )
    assert config.ollama.workers == (
        "https://192.168.1.20:11434", "https://192.168.1.21:11434",
    )


def test_remote_ollama_workers_require_https_with_consent():
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            env={
                "SONDER_ALLOW_REMOTE_OLLAMA": "1",
                "SONDER_OLLAMA_WORKERS": "http://192.168.1.20:11434",
            }
        )
    assert any("workers remote entries must use https" in e for e in excinfo.value.errors)


def _loopback_worker_list(total_workers: int) -> str:
    """Build a distinct primary-plus-worker roster without network access."""
    assert total_workers >= 1
    return ",".join(
        f"http://127.0.0.1:{12_000 + offset}"
        for offset in range(total_workers - 1)
    )


def test_ollama_static_roster_defaults_are_bounded():
    config = load_config(env=_CLEAN_ENV)

    assert config.ollama.worker_pool_max_workers == 16
    assert config.ollama.worker_capability_probe_parallelism == 4
    assert config.ollama.worker_capability_probe_batch_size == 32
    assert config.ollama.worker_status_page_size == 32


def test_ollama_config_preserves_preexisting_positional_argument_order():
    """Static-roster settings must not reinterpret legacy positional calls."""
    config = sonder_config.OllamaConfig(
        "http://127.0.0.1:12000",
        True,
        ("http://127.0.0.2:12000",),
        ("127.0.0.0/8",),
        7,
        90,
        1_001,
        4,
        30,
        300,
        2_000,
        60,
        301,
    )

    assert config.worker_max_inflight == 7
    assert config.worker_queue_depth == 90
    assert config.worker_admission_timeout_ms == 1_001
    assert config.worker_failure_threshold == 4
    assert config.worker_cooldown_seconds == 30
    assert config.worker_capability_ttl_seconds == 300
    assert config.worker_probe_timeout_ms == 2_000
    assert config.startup_timeout_seconds == 60
    assert config.request_timeout_seconds == 301
    assert config.worker_pool_max_workers == 16
    assert config.worker_capability_probe_parallelism == 4
    assert config.worker_capability_probe_batch_size == 32
    assert config.worker_status_page_size == 32


@pytest.mark.parametrize("maximum", [64, 256])
def test_typed_ollama_static_roster_capacity_includes_primary(tmp_path, maximum):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """[ollama]
worker_pool_max_workers = %d
worker_capability_probe_parallelism = 8
worker_capability_probe_batch_size = 128
worker_status_page_size = 128
workers = [%s]
""" % (
            maximum,
            ", ".join(
                '"http://127.0.0.1:%d"' % (12_000 + offset)
                for offset in range(maximum - 1)
            ),
        ),
        encoding="utf-8",
    )

    config = load_config(toml, env=_CLEAN_ENV)

    assert config.ollama.worker_pool_max_workers == maximum
    assert len(config.ollama.workers) + 1 == maximum
    assert config.ollama.worker_capability_probe_parallelism == 8
    assert config.ollama.worker_capability_probe_batch_size == 128
    assert config.ollama.worker_status_page_size == 128


def test_injected_ollama_static_roster_environment_overrides_toml(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """[ollama]
worker_pool_max_workers = 64
worker_capability_probe_parallelism = 2
worker_capability_probe_batch_size = 16
worker_status_page_size = 16
""",
        encoding="utf-8",
    )

    config = load_config(
        toml,
        env={
            "SONDER_OLLAMA_POOL_MAX_WORKERS": "256",
            "SONDER_OLLAMA_WORKER_PROBE_PARALLELISM": "8",
            "SONDER_OLLAMA_WORKER_PROBE_BATCH_SIZE": "128",
            "SONDER_OLLAMA_WORKER_STATUS_PAGE_SIZE": "128",
        },
    )

    assert config.ollama.worker_pool_max_workers == 256
    assert config.ollama.worker_capability_probe_parallelism == 8
    assert config.ollama.worker_capability_probe_batch_size == 128
    assert config.ollama.worker_status_page_size == 128


@pytest.mark.parametrize("maximum", [0, 257])
def test_typed_ollama_static_roster_capacity_rejects_out_of_range(tmp_path, maximum):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        "[ollama]\nworker_pool_max_workers = %d\n" % maximum,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(toml, env=_CLEAN_ENV)

    assert any("worker_pool_max_workers" in error for error in excinfo.value.errors)


@pytest.mark.parametrize("maximum, total_workers", [(64, 65), (256, 257)])
def test_injected_ollama_static_roster_rejects_one_worker_over_capacity(
    maximum, total_workers,
):
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            env={
                "SONDER_OLLAMA_POOL_MAX_WORKERS": str(maximum),
                "SONDER_OLLAMA_WORKERS": _loopback_worker_list(total_workers),
            },
        )

    assert any("worker_pool_max_workers" in error for error in excinfo.value.errors)


@pytest.mark.parametrize(
    "environment, expected",
    [
        (
            {
                "SONDER_OLLAMA_WORKERS": (
                    "http://127.0.0.2:11434,http://127.0.0.2:11434/"
                ),
            },
            "duplicate canonical worker",
        ),
        (
            {
                "OLLAMA_HOST": "http://localhost:11434",
                "SONDER_OLLAMA_WORKERS": "http://127.0.0.1:11434/",
            },
            "duplicates primary",
        ),
    ],
)
def test_ollama_static_roster_rejects_canonical_endpoint_collisions(
    environment, expected,
):
    with pytest.raises(ConfigError) as excinfo:
        load_config(env=environment)

    assert any(expected in error for error in excinfo.value.errors)


@pytest.mark.parametrize("variable, value, expected", [
    ("OLLAMA_HOST", "http://127.0.0.1:11434/api", "must be an origin"),
    ("OLLAMA_HOST", "http://user:pass@127.0.0.1:11434", "inline credentials"),
    ("SONDER_OLLAMA_WORKERS", "ftp://127.0.0.1:11434", "must use http or https"),
    ("SONDER_OLLAMA_WORKERS", "http://127.0.0.1:11434/api", "must be origins"),
    ("OLLAMA_HOST", "http://[::1", "invalid"),
    ("SONDER_OLLAMA_WORKERS", "http://127.0.0.1:not-a-port", "malformed"),
])
def test_ollama_origins_reject_ambiguous_or_credential_bearing_urls(
    variable, value, expected,
):
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={variable: value})
    assert any(expected in error for error in excinfo.value.errors)


def test_redacted_dump_never_contains_secret_values():
    secret = "extremely-secret-api-key-value-123"
    config = load_config(env={"SONDER_API_KEY": secret,
                              "SONDER_AUTH_SECRET": secret + "auth"})
    dumped = repr(config.as_redacted_dict())
    assert secret not in dumped
    assert config.as_redacted_dict()["secrets"]["api_key"] == "[set]"
