from __future__ import annotations

import pytest

from sonder_runtime.platform.config import ConfigError, load_config


def _enabled_rehearsal_toml(origin: str) -> str:
    return f'''\
[deployment]
profile = "pooled-pair"

[compute]
allow_remote = true
node_id = "node-a"

[[compute.nodes]]
id = "node-b"
origin = "https://node-b.example.test:11435"
workloads = ["service"]

[control_state_rehearsal]
enabled = true
cluster_id = "rehearsal-cluster"
node_id = "node-a"
witness_id = "witness-a"
provider_id = "provider-a"
origin = "{origin}"
timeout_seconds = 5
'''


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
