"""Configuration-load checks for opt-in llama.cpp RPC sharding (SONDER_RPC_*)."""
from __future__ import annotations

import pytest

from sonder_runtime.platform.config import ConfigError, load_config

# Placeholder private-range address; it belongs to no real deployment.
WORKER = "192.168.50.20"
LOOPBACK_URL = "http://127.0.0.1:11438"


def _errors(env: dict[str, str]) -> list[str]:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env=env)
    return list(excinfo.value.errors)


def test_unconfigured_sharding_is_inactive_and_loads() -> None:
    load_config(env={})
    load_config(env={"SONDER_RPC_PEERS": ""})
    # A non-loopback OpenAI-compatible URL is not this feature's business
    # until peers are configured.
    load_config(env={"SONDER_OPENAI_BASE_URL": "https://models.example.test"})


def test_configured_sharding_loads_with_private_peer_and_loopback_url() -> None:
    load_config(env={"SONDER_RPC_PEERS": f"{WORKER}:50052", "SONDER_OPENAI_BASE_URL": LOOPBACK_URL})
    load_config(env={
        "SONDER_RPC_PEERS": WORKER,
        "SONDER_RPC_PORT": "6000",
        "SONDER_OPENAI_BASE_URL": "http://localhost:11438",
    })


@pytest.mark.parametrize(
    ("peers", "fragment"),
    [
        ("203.0.113.9", "private"),
        ("8.8.8.8:50052", "private"),
        ("0.0.0.0", "unspecified"),
        ("127.0.0.1", "private"),
        ("worker.local", "hostnames"),
        ("fd00::1", "IPv4"),
        (f"{WORKER}:6000", "configured RPC port"),
        (f"{WORKER}:http", "decimal port"),
    ],
)
def test_peer_allowlist_rejections_fail_config_load(peers: str, fragment: str) -> None:
    errors = _errors({"SONDER_RPC_PEERS": peers, "SONDER_OPENAI_BASE_URL": LOOPBACK_URL})
    assert any(fragment in error for error in errors), errors


def test_invalid_rpc_port_fails_config_load_even_without_peers() -> None:
    errors = _errors({"SONDER_RPC_PORT": "70000"})
    assert any("SONDER_RPC_PORT" in error for error in errors), errors


@pytest.mark.parametrize(
    "base_url",
    ["", f"http://{WORKER}:11438", "https://models.example.test", "ftp://127.0.0.1:11438"],
)
def test_configured_peers_require_a_loopback_openai_base_url(base_url: str) -> None:
    env = {"SONDER_RPC_PEERS": WORKER}
    if base_url:
        env["SONDER_OPENAI_BASE_URL"] = base_url
    errors = _errors(env)
    assert any("SONDER_OPENAI_BASE_URL" in error for error in errors), errors


@pytest.mark.parametrize(
    "base_url",
    [
        "http://u:p@127.0.0.1:11438",
        "http://u@127.0.0.1:11438",
        "http://127.0.0.1:11438/?a=1",
        "http://127.0.0.1:11438#x",
        "http://127.0.0.1:99999",
        "http://127.0.0.1:0",
    ],
)
def test_configured_peers_reject_credentials_query_fragment_and_bad_port(base_url: str) -> None:
    errors = _errors({"SONDER_RPC_PEERS": WORKER, "SONDER_OPENAI_BASE_URL": base_url})
    assert any("SONDER_OPENAI_BASE_URL" in error for error in errors), errors


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:99999", "http://127.0.0.1:abc"])
def test_runtime_loopback_validator_rejects_bad_port(base_url: str) -> None:
    from sonder_runtime.adapters.inference.rpc_cluster import (
        RpcClusterConfigError,
        _validate_loopback_base_url,
    )

    with pytest.raises(RpcClusterConfigError):
        _validate_loopback_base_url(base_url)
