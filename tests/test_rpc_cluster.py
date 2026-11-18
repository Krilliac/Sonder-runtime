"""Tests for distributed llama.cpp RPC sharding helpers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sonder_runtime.adapters.inference.rpc_cluster import (
    DEFAULT_NODE1_PRIVATE_IP,
    DEFAULT_RPC_PORT,
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
    Node1RpcServerConfig,
    RpcClusterConfigError,
    RpcPeerEndpoint,
    ShardedLlamaServerConfig,
    current_780m_profile,
    discover_rpc_binaries,
    parse_rpc_peers,
    probe_rpc_peer,
    probe_sharded_server,
    resolve_ollama_gguf_blob,
    rx_9060_xt_16gb_profile,
    sonder_openai_env_for_rpc,
    validate_private_link_host,
    validate_server_bind_host,
    validate_tensor_split,
)


def test_private_link_host_validation_rejects_public_and_wildcard() -> None:
    assert validate_private_link_host("10.77.0.2") == "10.77.0.2"
    assert validate_private_link_host("10.77.0.1") == "10.77.0.1"

    for bad in ("", "0.0.0.0", "192.168.1.232", "127.0.0.1", "8.8.8.8", "10.77.0.0", "10.77.0.3", "node1"):
        with pytest.raises(RpcClusterConfigError):
            validate_private_link_host(bad)


def test_server_bind_host_must_be_loopback() -> None:
    assert validate_server_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_server_bind_host("localhost") == "127.0.0.1"
    assert validate_server_bind_host("::1") == "::1"

    for bad in ("", "0.0.0.0", "10.77.0.1", "192.168.1.10"):
        with pytest.raises(RpcClusterConfigError):
            validate_server_bind_host(bad)


def test_parse_rpc_peers_deduplicates_and_validates() -> None:
    peers = parse_rpc_peers("10.77.0.2:50052; 10.77.0.2, 10.77.0.1:50053")
    assert [p.address for p in peers] == ["10.77.0.2:50052", "10.77.0.1:50053"]
    assert parse_rpc_peers("") == ()

    with pytest.raises(RpcClusterConfigError):
        parse_rpc_peers("192.168.1.5:50052")
    with pytest.raises(RpcClusterConfigError):
        RpcPeerEndpoint.parse("10.77.0.2:99999")


def test_node1_rpc_server_args() -> None:
    cfg = Node1RpcServerConfig(device="Vulkan0", threads=8, use_cache=True)
    assert cfg.endpoint.address == f"{DEFAULT_NODE1_PRIVATE_IP}:{DEFAULT_RPC_PORT}"
    assert cfg.build_args() == [
        "--host",
        "10.77.0.2",
        "--port",
        "50052",
        "--device",
        "Vulkan0",
        "--threads",
        "8",
        "-c",
    ]


def test_sharded_llama_server_args_and_validation() -> None:
    cfg = ShardedLlamaServerConfig(
        model_path=r"D:\ollama\gguf\Qwen3.8-27B.gguf",
        tensor_split="16,16",
        ctx_size=16384,
        n_gpu_layers=99,
    )
    assert cfg.base_url == f"http://{DEFAULT_SERVER_HOST}:{DEFAULT_SERVER_PORT}"
    args = cfg.build_args()
    assert "--rpc" in args
    assert args[args.index("--rpc") + 1] == "10.77.0.2:50052"
    assert args[args.index("--tensor-split") + 1] == "16,16"
    assert args[args.index("--cache-type-k") + 1] == "q8_0"
    assert args[args.index("--cache-type-v") + 1] == "q8_0"
    assert args[args.index("--flash-attn") + 1] == "on"
    assert "--no-webui" in args

    with pytest.raises(RpcClusterConfigError):
        ShardedLlamaServerConfig(model_path="model.gguf", host="0.0.0.0")
    with pytest.raises(RpcClusterConfigError):
        ShardedLlamaServerConfig(model_path="model.gguf", cache_type_k="invalid")
    with pytest.raises(RpcClusterConfigError):
        validate_tensor_split("0,0")


def test_probe_rpc_peer_uses_injected_connector() -> None:
    calls: list[tuple[tuple[str, int], float]] = []

    def ok_connect(addr: tuple[str, int], timeout: float) -> None:
        calls.append((addr, timeout))

    health = probe_rpc_peer("10.77.0.2:50052", timeout=1.5, connect_fn=ok_connect)
    assert health.reachable is True
    assert health.endpoint == "10.77.0.2:50052"
    assert calls == [(("10.77.0.2", 50052), 1.5)]

    def fail_connect(addr: tuple[str, int], timeout: float) -> None:
        raise OSError("connection refused")

    bad = probe_rpc_peer("10.77.0.2:50052", connect_fn=fail_connect)
    assert bad.reachable is False
    assert "refused" in bad.error


def test_probe_sharded_server_reads_health_and_models() -> None:
    def fake_get(url: str, timeout: float) -> tuple[int, bytes]:
        if url.endswith("/health"):
            return 200, b'{"status":"ok"}'
        if url.endswith("/v1/models"):
            return 200, b'{"data":[{"id":"sonder-rpc"}]}'
        raise AssertionError(url)

    ok = probe_sharded_server("http://127.0.0.1:11438", get_fn=fake_get)
    assert ok.healthy is True
    assert ok.status == "ok"
    assert ok.model_ids == ("sonder-rpc",)

    with pytest.raises(RpcClusterConfigError):
        probe_sharded_server("http://10.77.0.2:11438", get_fn=fake_get)


def test_discover_rpc_binaries_and_ollama_blob_resolution(tmp_path: Path) -> None:
    rpc_dir = tmp_path / "llama-rpc"
    rpc_dir.mkdir()
    (rpc_dir / "llama-server.exe").write_bytes(b"")
    (rpc_dir / "ggml-rpc-server.exe").write_bytes(b"")
    found = discover_rpc_binaries(rpc_dir)
    assert found["llama_server"] == rpc_dir / "llama-server.exe"
    assert found["rpc_server"] == rpc_dir / "ggml-rpc-server.exe"

    models_dir = tmp_path / "ollama_models"
    manifest_dir = models_dir / "manifests" / "registry.ollama.ai" / "library" / "demo"
    blobs_dir = models_dir / "blobs"
    manifest_dir.mkdir(parents=True)
    blobs_dir.mkdir(parents=True)
    digest = "sha256:abcdef0123456789"
    blob_file = blobs_dir / "sha256-abcdef0123456789"
    blob_file.write_bytes(b"GGUF")
    (manifest_dir / "latest").write_text(
        json.dumps(
            {
                "layers": [
                    {"mediaType": "application/vnd.ollama.image.model", "digest": digest}
                ]
            }
        ),
        encoding="utf-8",
    )
    resolved = resolve_ollama_gguf_blob("demo:latest", models_dir=models_dir)
    assert resolved == blob_file


def test_sonder_openai_env_and_hardware_profiles() -> None:
    env = sonder_openai_env_for_rpc(
        "http://127.0.0.1:11438",
        model_alias="sonder-rpc",
        tiers=("general", "reasoning"),
        rpc_peers=("10.77.0.2:50052",),
    )
    assert env == {
        "SONDER_OPENAI_BASE_URL": "http://127.0.0.1:11438",
        "SONDER_OPENAI_MODEL": "sonder-rpc",
        "SONDER_RPC_PEERS": "10.77.0.2:50052",
        "SONDER_GENERAL_PROVIDER": "openai-compatible",
        "SONDER_REASONING_PROVIDER": "openai-compatible",
    }
    assert current_780m_profile().tensor_split == "14,2"
    assert rx_9060_xt_16gb_profile().tensor_split == "16,16"
