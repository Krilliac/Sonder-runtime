"""Regression tests for the RPC sharding readiness probe, config check and profile loading."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from sonder_runtime.adapters.inference import rpc_cluster
from sonder_runtime.adapters.inference.rpc_cluster import (
    RpcClusterConfigError,
    ShardedServerHealth,
    current_780m_profile,
    load_hardware_profiles,
    log_sharded_readiness,
    log_sharded_readiness_from_env,
    probe_sharded_server,
    rx_9060_xt_16gb_profile,
)
from sonder_runtime.adapters.provider_bindings import normalize_provider
from sonder_runtime.platform.config import ConfigError, load_config


def _fake_get(url: str, timeout: float) -> tuple[int, bytes]:
    if url.endswith("/health"):
        return 200, b'{"status":"ok"}'
    return 200, b'{"data":[{"id":"sonder-rpc"}]}'


def test_readiness_logs_healthy_backend(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.rpc.readiness")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_sharded_readiness(
            logger,
            "http://127.0.0.1:11438",
            probe=lambda url: probe_sharded_server(url, get_fn=_fake_get),
        )
    assert "healthy=True models=['sonder-rpc']" in caplog.text
    assert "probe failed" not in caplog.text


def test_readiness_warns_when_backend_unhealthy(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.rpc.readiness.bad")

    def down(_url: str) -> ShardedServerHealth:
        return ShardedServerHealth(base_url="http://127.0.0.1:11438", healthy=False, error="refused")

    with caplog.at_level(logging.INFO, logger=logger.name):
        log_sharded_readiness(logger, "http://127.0.0.1:11438", probe=down)
    assert "healthy=False" in caplog.text
    assert "refused" in caplog.text


def test_readiness_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.rpc.readiness.raise")

    def boom(_url: str) -> ShardedServerHealth:
        raise RpcClusterConfigError("not loopback")

    with caplog.at_level(logging.INFO, logger=logger.name):
        log_sharded_readiness(logger, "http://10.77.0.2:1", probe=boom)
    assert "probe failed (RpcClusterConfigError)" in caplog.text


def _healthy(url: str) -> ShardedServerHealth:
    return ShardedServerHealth(base_url=url, healthy=True, model_ids=("sonder-rpc",))


def test_startup_probe_runs_only_when_base_url_and_peers_are_set() -> None:
    logger = logging.getLogger("test.rpc.startup")
    seen: list[str] = []

    def probe(url: str) -> ShardedServerHealth:
        seen.append(url)
        return _healthy(url)

    both = {"SONDER_OPENAI_BASE_URL": " http://127.0.0.1:11438 ", "SONDER_RPC_PEERS": "10.77.0.2:50052"}
    assert log_sharded_readiness_from_env(logger, both, probe=probe) is True
    assert seen == ["http://127.0.0.1:11438"]
    for env in (
        {},
        {"SONDER_OPENAI_BASE_URL": "http://127.0.0.1:11438"},
        {"SONDER_RPC_PEERS": "10.77.0.2:50052"},
        {"SONDER_OPENAI_BASE_URL": "  ", "SONDER_RPC_PEERS": "10.77.0.2:50052"},
    ):
        assert log_sharded_readiness_from_env(logger, env, probe=probe) is False
    assert len(seen) == 1


def test_serve_startup_calls_env_readiness_helper() -> None:
    serve = Path(__file__).resolve().parents[1] / "sonder_runtime" / "interfaces" / "http" / "serve.py"
    source = serve.read_text(encoding="utf-8")
    assert "log_sharded_readiness_from_env(_serve_logger, os.environ)" in source


def test_config_rejects_rpc_peers_outside_private_link_from_env() -> None:
    with pytest.raises(ConfigError) as info:
        load_config(env={"SONDER_RPC_PEERS": "192.168.1.5:50052"})
    assert "outside private link" in " ".join(info.value.errors)


def test_config_reads_rpc_peers_from_os_environ_when_env_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SONDER_RPC_PEERS", "192.168.1.5:50052")
    with pytest.raises(ConfigError) as info:
        load_config()
    assert "outside private link" in " ".join(info.value.errors)
    monkeypatch.setenv("SONDER_RPC_PEERS", "10.77.0.2:50052")
    load_config()


def test_config_rejects_rpc_peers_from_secrets_file(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets.env"
    secrets.write_text("SONDER_RPC_PEERS=8.8.8.8:50052\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(secrets_path=secrets, env={})
    assert "outside private link" in " ".join(info.value.errors)


def test_config_rejects_bad_ports_and_hosts() -> None:
    for bad in ("10.77.0.2:abc", "10.77.0.2:0", "10.77.0.3:50052", "::1"):
        with pytest.raises(ConfigError):
            load_config(env={"SONDER_RPC_PEERS": bad})


def test_config_accepts_private_link_peers() -> None:
    load_config(env={"SONDER_RPC_PEERS": "10.77.0.2:50052,10.77.0.1"})


def test_provider_aliases_map_to_openai_compatible() -> None:
    assert normalize_provider("distributed-rpc") == "openai_compatible"
    assert normalize_provider("rpc-sharded") == "openai_compatible"


def test_hardware_profiles_load_and_validate() -> None:
    assert current_780m_profile().tensor_split == "14,2"
    assert rx_9060_xt_16gb_profile().ctx_size == 16384
    assert set(load_hardware_profiles()) >= {
        "main-5070ti-plus-node1-780m",
        "main-5070ti-plus-node1-9060xt-16gb",
    }


def test_hardware_profiles_reject_malformed_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rpc_cluster, "load_inference_config", lambda: {"profiles": {"p": {"node1_device": "Vulkan0"}}}
    )
    with pytest.raises(RpcClusterConfigError):
        load_hardware_profiles()
    monkeypatch.setattr(rpc_cluster, "load_inference_config", lambda: {"profiles": {}})
    with pytest.raises(RpcClusterConfigError):
        current_780m_profile()


def test_inference_config_must_be_object(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real_open = open
    bad = tmp_path / "inference_models.json"
    bad.write_text(json.dumps([1, 2]), encoding="utf-8")
    monkeypatch.setattr("builtins.open", lambda *_a, **_k: real_open(bad, encoding="utf-8"))
    with pytest.raises(RpcClusterConfigError):
        rpc_cluster.load_inference_config()


def test_default_install_dir_derives_from_localappdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert rpc_cluster._default_main_install_dir() == tmp_path / "sonder" / "llama-rpc"
