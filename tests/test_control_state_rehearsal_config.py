from __future__ import annotations

import pytest

from sonder_runtime.platform.config import ConfigError, load_config


def _enabled_rehearsal_toml(
    origin: str,
    *,
    profile: str = "pooled-pair",
    node_id: str = "node-a",
    witness_id: str = "witness-a",
    timeout_seconds: int = 5,
    allow_insecure_loopback: bool = False,
) -> str:
    return f'''\
[deployment]
profile = "{profile}"

[compute]
allow_remote = true
node_id = "{node_id}"

[[compute.nodes]]
id = "node-b"
origin = "https://node-b.example.test:11435"
workloads = ["service"]

[control_state_rehearsal]
enabled = true
cluster_id = "rehearsal-cluster"
node_id = "node-a"
witness_id = "{witness_id}"
provider_id = "provider-a"
origin = "{origin}"
timeout_seconds = {timeout_seconds}
allow_insecure_loopback = {str(allow_insecure_loopback).lower()}
'''


def _load_enabled(path, *, secret: str = "rehearsal-secret", **changes):
    path.write_text(
        _enabled_rehearsal_toml(**changes),
        encoding="utf-8",
    )
    return load_config(
        path,
        env={
            "SONDER_API_KEY": "x" * 24,
            "SONDER_CONTROL_STATE_REHEARSAL_API_KEY": secret,
        },
    )


def test_default_rehearsal_is_disabled_and_has_no_secret_in_redacted_config() -> None:
    # Catches an accidental default-on rehearsal or a secret-bearing config dump.
    config = load_config(env={})

    assert config.control_state_rehearsal.enabled is False
    assert config.secrets.as_redacted_dict()["control_state_rehearsal_key"] == "[unset]"


def test_enabled_rehearsal_rejects_plaintext_remote_origin_and_missing_key(tmp_path) -> None:
    # Catches an enabled remote rehearsal that permits plaintext or anonymous requests.
    path = tmp_path / "sonder.toml"
    path.write_text(
        _enabled_rehearsal_toml("http://control.example.test:9443"), encoding="utf-8"
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={"SONDER_API_KEY": "x" * 24})

    errors = "\n".join(excinfo.value.errors)
    assert "HTTPS" in errors
    assert "SONDER_CONTROL_STATE_REHEARSAL_API_KEY" in errors


def test_enabled_rehearsal_loads_with_https_origin(tmp_path) -> None:
    # Catches rejection of the normal authenticated TLS rehearsal boundary.
    config = _load_enabled(
        tmp_path / "sonder.toml",
        origin="https://control.example.test:9443",
    )

    assert config.control_state_rehearsal.origin == "https://control.example.test:9443"


def test_enabled_rehearsal_loads_with_explicit_loopback_http(tmp_path) -> None:
    # Catches loss of the explicit disposable loopback-provider test path.
    config = _load_enabled(
        tmp_path / "sonder.toml",
        origin="http://127.0.0.1:9443",
        allow_insecure_loopback=True,
    )

    assert config.control_state_rehearsal.allow_insecure_loopback is True


def test_enabled_rehearsal_rejects_loopback_http_without_explicit_flag(tmp_path) -> None:
    # Catches plaintext loopback becoming accepted without an operator opt-in.
    path = tmp_path / "sonder.toml"
    path.write_text(
        _enabled_rehearsal_toml("http://127.0.0.1:9443"), encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="must use HTTPS"):
        load_config(
            path,
            env={
                "SONDER_API_KEY": "x" * 24,
                "SONDER_CONTROL_STATE_REHEARSAL_API_KEY": "rehearsal-secret",
            },
        )


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"profile": "single-host"}, "requires [deployment].profile"),
        ({"node_id": "node-c"}, "node_id must match [compute].node_id"),
        ({"witness_id": "node-b"}, "witness_id must differ"),
        ({"timeout_seconds": 31}, "timeout_seconds must be within 1..30"),
    ],
)
def test_enabled_rehearsal_rejects_invalid_topology_or_timeout(
    tmp_path, changes, expected
) -> None:
    # Catches a rehearsal that is not bounded to the declared two-node topology.
    path = tmp_path / "sonder.toml"
    path.write_text(
        _enabled_rehearsal_toml("https://control.example.test:9443", **changes),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(
            path,
            env={
                "SONDER_API_KEY": "x" * 24,
                "SONDER_CONTROL_STATE_REHEARSAL_API_KEY": "rehearsal-secret",
            },
        )

    assert expected in "\n".join(excinfo.value.errors)


def test_rehearsal_secret_is_environment_only_and_redacted(tmp_path) -> None:
    # Catches the dedicated credential entering TOML, overrides, repr, or diagnostics.
    secret = "rehearsal-secret-value"
    path = tmp_path / "sonder.toml"
    config = _load_enabled(
        path,
        origin="https://control.example.test:9443",
        secret=secret,
    )

    assert config.secrets.control_state_rehearsal_key == secret
    assert secret not in repr(config.secrets)
    assert config.secrets.as_redacted_dict()["control_state_rehearsal_key"] == "[set]"

    path.write_text(
        _enabled_rehearsal_toml("https://control.example.test:9443")
        + 'control_state_rehearsal_key = "toml-secret"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="may not appear in TOML"):
        load_config(
            path,
            env={
                "SONDER_API_KEY": "x" * 24,
                "SONDER_CONTROL_STATE_REHEARSAL_API_KEY": secret,
            },
        )
    with pytest.raises(ConfigError, match="invalid override key"):
        load_config(
            env={
                "SONDER_API_KEY": "x" * 24,
                "SONDER_CONTROL_STATE_REHEARSAL_API_KEY": secret,
            },
            overrides={"control_state_rehearsal.control_state_rehearsal_key": secret},
        )
