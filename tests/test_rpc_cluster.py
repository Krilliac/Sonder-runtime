"""Tests for opt-in llama.cpp RPC sharding helpers and their fail-closed policy."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sonder_runtime.adapters.inference.rpc_cluster import (
    DEFAULT_PROFILES_PATH,
    DEFAULT_RPC_PORT,
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
    RpcClusterConfigError,
    RpcPeerEndpoint,
    RpcWorkerServerConfig,
    ShardedLlamaServerConfig,
    default_rpc_install_dir,
    discover_rpc_binaries,
    get_hardware_profile,
    load_hardware_profiles,
    parse_rpc_peers,
    probe_configured_sharded_server,
    probe_rpc_peer,
    probe_sharded_server,
    resolve_ollama_gguf_blob,
    rpc_sharding_settings_from_env,
    run_sharded_backend_probe,
    sonder_openai_env_for_rpc,
    start_sharded_backend_probe,
    validate_peer_host,
    validate_server_bind_host,
    validate_tensor_split,
)
from sonder_runtime.platform.rpc_sharding_config import (
    RpcShardingConfigError,
    rpc_peer_allowlist,
    rpc_sharding_errors,
)

# Placeholder private-range addresses; none belongs to a real deployment.
WORKER = "192.168.50.20"
WORKER_2 = "10.20.30.40"
LINK_LOCAL = "169.254.10.2"


def test_peer_host_accepts_private_and_link_local_literals() -> None:
    assert validate_peer_host(WORKER) == WORKER
    assert validate_peer_host(WORKER_2) == WORKER_2
    assert validate_peer_host("172.16.0.5") == "172.16.0.5"
    assert validate_peer_host(f" {LINK_LOCAL} ") == LINK_LOCAL


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "0.0.0.0",
        "127.0.0.1",
        "localhost",
        "worker.local",
        "rpc-worker",
        "8.8.8.8",
        "100.64.0.1",
        "172.32.0.1",
        "192.0.2.10",
        "198.51.100.7",
        "224.0.0.1",
        "255.255.255.255",
        "fe80::1",
        "fd00::1",
        "::",
        "10.0.0.1/24",
        "010.0.0.1",
    ],
)
def test_peer_host_rejects_everything_else(bad: str) -> None:
    with pytest.raises(RpcClusterConfigError):
        validate_peer_host(bad)


def test_cluster_and_platform_raise_the_same_error_type() -> None:
    assert RpcClusterConfigError is RpcShardingConfigError
    assert issubclass(RpcClusterConfigError, ValueError)


def test_server_bind_host_must_be_loopback() -> None:
    assert validate_server_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_server_bind_host("localhost") == "127.0.0.1"
    assert validate_server_bind_host("::1") == "::1"

    for bad in ("", "0.0.0.0", "::", WORKER, "192.168.1.10", "example.test"):
        with pytest.raises(RpcClusterConfigError):
            validate_server_bind_host(bad)


def test_parse_rpc_peers_deduplicates_and_applies_configured_port() -> None:
    peers = parse_rpc_peers(f"{WORKER}:50052; {WORKER}, {WORKER_2}")
    assert [p.address for p in peers] == [f"{WORKER}:50052", f"{WORKER_2}:50052"]
    assert parse_rpc_peers("") == ()
    assert parse_rpc_peers(" , ; ") == ()

    custom = parse_rpc_peers(f"{WORKER}:6000,{WORKER_2}", port=6000)
    assert [p.address for p in custom] == [f"{WORKER}:6000", f"{WORKER_2}:6000"]


@pytest.mark.parametrize(
    "raw",
    [
        "192.0.2.10:50052",
        "8.8.8.8",
        "0.0.0.0:50052",
        "worker.local:50052",
        f"{WORKER}:50053",
        f"{WORKER}:99999",
        f"{WORKER}:",
        f"{WORKER}:abc",
        "[fd00::1]:50052",
        "fd00::1",
        f"{WORKER},127.0.0.1",
    ],
)
def test_parse_rpc_peers_rejects_entries_outside_the_policy(raw: str) -> None:
    with pytest.raises(RpcClusterConfigError):
        parse_rpc_peers(raw)


def test_peer_allowlist_refuses_too_many_entries() -> None:
    raw = ",".join(f"10.0.0.{i}" for i in range(1, 18))
    with pytest.raises(RpcClusterConfigError, match="at most"):
        parse_rpc_peers(raw)


def test_not_configured_means_inactive() -> None:
    assert rpc_sharding_settings_from_env({}) is None
    assert rpc_sharding_settings_from_env({"SONDER_RPC_PEERS": "  "}) is None
    # Other sharding-adjacent settings alone never turn it on.
    assert rpc_sharding_settings_from_env({
        "SONDER_OPENAI_BASE_URL": "http://127.0.0.1:11438",
        "SONDER_RPC_PORT": "50052",
        "SONDER_GENERAL_PROVIDER": "rpc-sharded",
    }) is None
    assert probe_configured_sharded_server({}, get_fn=_unreachable_get) is None
    allowlist = rpc_peer_allowlist({})
    assert allowlist.active is False
    assert allowlist.peers == ()
    assert allowlist.port == DEFAULT_RPC_PORT
    assert rpc_sharding_errors({}) == []


def test_configured_settings_validate_peers_port_and_loopback_url() -> None:
    settings = rpc_sharding_settings_from_env({
        "SONDER_RPC_PEERS": f"{WORKER},{LINK_LOCAL}:6000",
        "SONDER_RPC_PORT": "6000",
        "SONDER_OPENAI_BASE_URL": "http://127.0.0.1:11438/",
    })
    assert settings is not None
    assert [p.address for p in settings.peers] == [f"{WORKER}:6000", f"{LINK_LOCAL}:6000"]
    assert settings.rpc_port == 6000
    assert settings.server_url == "http://127.0.0.1:11438"


@pytest.mark.parametrize(
    ("env", "fragment"),
    [
        ({"SONDER_RPC_PEERS": "203.0.113.9"}, "private"),
        ({"SONDER_RPC_PEERS": "worker.local"}, "hostnames"),
        ({"SONDER_RPC_PEERS": "0.0.0.0"}, "unspecified"),
        ({"SONDER_RPC_PEERS": f"{WORKER}:6000"}, "configured RPC port"),
        ({"SONDER_RPC_PEERS": WORKER, "SONDER_RPC_PORT": "0"}, "between 1 and 65535"),
        ({"SONDER_RPC_PEERS": WORKER, "SONDER_RPC_PORT": "fifty"}, "decimal port"),
        ({"SONDER_RPC_PORT": "70000"}, "between 1 and 65535"),
    ],
)
def test_allowlist_rejections_carry_a_clear_error(env: dict[str, str], fragment: str) -> None:
    with pytest.raises(RpcClusterConfigError, match=fragment):
        rpc_peer_allowlist(env)
    errors = rpc_sharding_errors(env)
    assert len(errors) == 1 and fragment in errors[0]


def test_configured_peers_need_a_loopback_server_url() -> None:
    with pytest.raises(RpcClusterConfigError, match="SONDER_OPENAI_BASE_URL"):
        rpc_sharding_settings_from_env({"SONDER_RPC_PEERS": WORKER})
    with pytest.raises(RpcClusterConfigError, match="loopback"):
        rpc_sharding_settings_from_env({
            "SONDER_RPC_PEERS": WORKER,
            "SONDER_OPENAI_BASE_URL": f"http://{WORKER}:11438",
        })


def test_rpc_worker_server_args_require_an_explicit_private_host() -> None:
    cfg = RpcWorkerServerConfig(host=WORKER, device="Vulkan0", threads=8, use_cache=True)
    assert cfg.endpoint.address == f"{WORKER}:{DEFAULT_RPC_PORT}"
    assert cfg.build_args() == [
        "--host",
        WORKER,
        "--port",
        "50052",
        "--device",
        "Vulkan0",
        "--threads",
        "8",
        "-c",
    ]
    for bad_host in ("0.0.0.0", "127.0.0.1", "192.0.2.1", "worker.local"):
        with pytest.raises(RpcClusterConfigError):
            RpcWorkerServerConfig(host=bad_host)
    with pytest.raises(RpcClusterConfigError):
        RpcWorkerServerConfig(host=WORKER, threads=-1)


def test_sharded_llama_server_args_and_validation() -> None:
    peers = (RpcPeerEndpoint(WORKER),)
    cfg = ShardedLlamaServerConfig(
        model_path="model.gguf",
        rpc_peers=peers,
        tensor_split="1,1",
        ctx_size=16384,
        n_gpu_layers=99,
    )
    assert cfg.base_url == f"http://{DEFAULT_SERVER_HOST}:{DEFAULT_SERVER_PORT}"
    args = cfg.build_args()
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert args[args.index("--rpc") + 1] == f"{WORKER}:50052"
    assert args[args.index("--tensor-split") + 1] == "1,1"
    assert args[args.index("--cache-type-k") + 1] == "q8_0"
    assert args[args.index("--cache-type-v") + 1] == "q8_0"
    assert args[args.index("--flash-attn") + 1] == "on"
    assert "--no-webui" in args

    with pytest.raises(RpcClusterConfigError):
        ShardedLlamaServerConfig(model_path="model.gguf", rpc_peers=peers, host="0.0.0.0")
    with pytest.raises(RpcClusterConfigError):
        ShardedLlamaServerConfig(model_path="model.gguf", rpc_peers=peers, host=WORKER)
    with pytest.raises(RpcClusterConfigError):
        ShardedLlamaServerConfig(model_path="model.gguf", rpc_peers=())
    with pytest.raises(RpcClusterConfigError):
        ShardedLlamaServerConfig(
            model_path="model.gguf",
            rpc_peers=(RpcPeerEndpoint(WORKER), RpcPeerEndpoint(WORKER_2, 6000)),
        )
    with pytest.raises(RpcClusterConfigError):
        ShardedLlamaServerConfig(model_path="model.gguf", rpc_peers=peers, cache_type_k="invalid")
    for extra in (("--host", "0.0.0.0"), ("--HOST=0.0.0.0",), ("--port", "80"), ("--rpc", "8.8.8.8:50052")):
        with pytest.raises(RpcClusterConfigError, match="extra_args"):
            ShardedLlamaServerConfig(model_path="model.gguf", rpc_peers=peers, extra_args=extra)
    with pytest.raises(RpcClusterConfigError):
        validate_tensor_split("0,0")
    with pytest.raises(RpcClusterConfigError):
        validate_tensor_split("1,nan")


def test_rpc_peer_endpoint_has_no_default_host() -> None:
    with pytest.raises(TypeError):
        RpcPeerEndpoint()  # type: ignore[call-arg]


def test_probe_rpc_peer_uses_injected_connector() -> None:
    calls: list[tuple[tuple[str, int], float]] = []

    def ok_connect(addr: tuple[str, int], timeout: float) -> None:
        calls.append((addr, timeout))

    health = probe_rpc_peer(f"{WORKER}:50052", timeout=1.5, connect_fn=ok_connect)
    assert health.reachable is True
    assert health.endpoint == f"{WORKER}:50052"
    assert calls == [((WORKER, 50052), 1.5)]

    def fail_connect(addr: tuple[str, int], timeout: float) -> None:
        raise OSError("connection refused")

    bad = probe_rpc_peer(f"{WORKER}:50052", connect_fn=fail_connect)
    assert bad.reachable is False
    assert "refused" in bad.error

    with pytest.raises(RpcClusterConfigError):
        probe_rpc_peer("8.8.8.8:50052", connect_fn=ok_connect)
    assert len(calls) == 1


def _fake_get(url: str, timeout: float) -> tuple[int, bytes]:
    if url.endswith("/health"):
        return 200, b'{"status":"ok"}'
    if url.endswith("/v1/models"):
        return 200, b'{"data":[{"id":"sonder-rpc"}]}'
    raise AssertionError(url)


def _unreachable_get(url: str, timeout: float) -> tuple[int, bytes]:
    raise AssertionError(f"no probe expected, got {url}")


def test_probe_sharded_server_reads_health_and_models() -> None:
    ok = probe_sharded_server("http://127.0.0.1:11438", get_fn=_fake_get)
    assert ok.healthy is True
    assert ok.status == "ok"
    assert ok.model_ids == ("sonder-rpc",)

    with pytest.raises(RpcClusterConfigError):
        probe_sharded_server(f"http://{WORKER}:11438", get_fn=_fake_get)


def test_probe_configured_sharded_server_uses_env_settings() -> None:
    health = probe_configured_sharded_server(
        {"SONDER_RPC_PEERS": WORKER, "SONDER_OPENAI_BASE_URL": "http://127.0.0.1:11438"},
        get_fn=_fake_get,
    )
    assert health is not None and health.healthy is True


def test_discover_rpc_binaries_and_ollama_blob_resolution(tmp_path: Path) -> None:
    rpc_dir = tmp_path / "llama-rpc"
    rpc_dir.mkdir()
    (rpc_dir / "llama-server.exe").write_bytes(b"")
    (rpc_dir / "ggml-rpc-server.exe").write_bytes(b"")
    found = discover_rpc_binaries(rpc_dir)
    assert found["llama_server"] == rpc_dir / "llama-server.exe"
    assert found["rpc_server"] == rpc_dir / "ggml-rpc-server.exe"

    from_env = discover_rpc_binaries(env={"SONDER_RPC_INSTALL_DIR": str(rpc_dir)})
    assert from_env["install_dir"] == rpc_dir

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
    with pytest.raises(RpcClusterConfigError):
        resolve_ollama_gguf_blob("../demo:latest", models_dir=models_dir)


def test_default_install_dir_comes_from_configuration(tmp_path: Path) -> None:
    configured = tmp_path / "custom"
    assert default_rpc_install_dir({"SONDER_RPC_INSTALL_DIR": str(configured)}) == configured
    assert default_rpc_install_dir({"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "sonder" / "llama-rpc"


def test_sonder_openai_env_requires_explicit_peers() -> None:
    env = sonder_openai_env_for_rpc(
        "http://127.0.0.1:11438",
        model_alias="sonder-rpc",
        tiers=("general", "reasoning"),
        rpc_peers=(f"{WORKER}:50052",),
    )
    assert env == {
        "SONDER_OPENAI_BASE_URL": "http://127.0.0.1:11438",
        "SONDER_OPENAI_MODEL": "sonder-rpc",
        "SONDER_RPC_PEERS": f"{WORKER}:50052",
        "SONDER_RPC_PORT": "50052",
        "SONDER_GENERAL_PROVIDER": "openai-compatible",
        "SONDER_REASONING_PROVIDER": "openai-compatible",
    }
    with pytest.raises(RpcClusterConfigError):
        sonder_openai_env_for_rpc(rpc_peers=())
    with pytest.raises(RpcClusterConfigError):
        sonder_openai_env_for_rpc(rpc_peers=("192.0.2.10",))
    with pytest.raises(RpcClusterConfigError):
        sonder_openai_env_for_rpc(rpc_peers=(RpcPeerEndpoint(WORKER, 6000),))
    with pytest.raises(RpcClusterConfigError):
        sonder_openai_env_for_rpc(rpc_peers=(WORKER,), tiers=("nope",))


def test_shipped_hardware_profiles_are_generic_and_valid() -> None:
    profiles = load_hardware_profiles(DEFAULT_PROFILES_PATH)
    assert set(profiles) == {"balanced-equal-vram", "main-heavy-small-worker"}
    assert get_hardware_profile("balanced-equal-vram", path=DEFAULT_PROFILES_PATH).tensor_split == "1,1"
    assert profiles["main-heavy-small-worker"].tensor_split == "7,1"
    with pytest.raises(RpcClusterConfigError, match="unknown RPC profile"):
        get_hardware_profile("missing", path=DEFAULT_PROFILES_PATH)


def test_hardware_profiles_file_is_validated(tmp_path: Path) -> None:
    good = tmp_path / "profiles.json"
    good.write_text(json.dumps({"profiles": {"custom": {
        "worker_device": "Vulkan1",
        "tensor_split": "3,1",
        "ctx_size": 8192,
        "n_gpu_layers": 80,
        "notes": "operator profile",
    }}}), encoding="utf-8")
    loaded = load_hardware_profiles(env={"SONDER_RPC_PROFILES_FILE": str(good)})
    assert loaded["custom"].worker_device == "Vulkan1"

    bad_documents = [
        {"profiles": {"x": {"tensor_split": "1,1"}}},
        {"profiles": {"x": {
            "worker_device": "", "tensor_split": "1,1", "ctx_size": 64,
            "n_gpu_layers": 1, "notes": "",
        }}},
        {"profiles": {"x": {
            "worker_device": "", "tensor_split": "1,1", "ctx_size": 4096,
            "n_gpu_layers": 1, "notes": "", "host": "8.8.8.8",
        }}},
        {"profiles": {}, "models": []},
        ["not", "an", "object"],
    ]
    for index, document in enumerate(bad_documents):
        path = tmp_path / f"bad{index}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(RpcClusterConfigError):
            load_hardware_profiles(path)
    with pytest.raises(RpcClusterConfigError):
        load_hardware_profiles(tmp_path / "missing.json")


class _RecordingThread:
    def __init__(self, calls: list[dict[str, object]], **kwargs: object) -> None:
        self._kwargs = kwargs
        calls.append(kwargs)

    def start(self) -> None:
        target = self._kwargs["target"]
        args = self._kwargs.get("args", ())
        assert callable(target) and isinstance(args, tuple)
        target(*args)


def test_startup_probe_is_inactive_without_configured_peers() -> None:
    calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> _RecordingThread:
        return _RecordingThread(calls, **kwargs)

    assert start_sharded_backend_probe(thread_factory=factory, env={}) is False
    assert start_sharded_backend_probe(
        thread_factory=factory,
        env={"SONDER_RPC_PEERS": " ", "SONDER_OPENAI_BASE_URL": "http://127.0.0.1:11438"},
    ) is False
    assert calls == []


def test_startup_probe_runs_once_on_a_daemon_thread_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sonder_runtime.adapters.inference.rpc_cluster as rpc_cluster

    probed: list[str] = []

    def fake_probe(url: str, *, timeout: float = 3.0, get_fn: object = None) -> object:
        probed.append(url)
        return probe_sharded_server(url, get_fn=_fake_get)

    monkeypatch.setattr(rpc_cluster, "probe_sharded_server", fake_probe)
    calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> _RecordingThread:
        return _RecordingThread(calls, **kwargs)

    env = {"SONDER_RPC_PEERS": WORKER, "SONDER_OPENAI_BASE_URL": "http://127.0.0.1:11438"}
    assert start_sharded_backend_probe(thread_factory=factory, env=env) is True
    assert len(calls) == 1 and calls[0]["daemon"] is True
    assert probed == ["http://127.0.0.1:11438"]


def test_startup_probe_never_raises_on_bad_configuration() -> None:
    assert run_sharded_backend_probe({"SONDER_RPC_PEERS": "8.8.8.8"}) is None
    assert run_sharded_backend_probe(
        {"SONDER_RPC_PEERS": WORKER, "SONDER_OPENAI_BASE_URL": "http://127.0.0.1:11438"},
        get_fn=_fake_get,
    ) is not None


@pytest.mark.parametrize(
    "name",
    ["demo:a/../../x", "demo:a/b", "\\\\srv\\share\\x:latest", "C:\\x:latest", "demo:la test", "-demo:latest", "demo:.hidden"],
)
def test_ollama_model_ref_rejects_unsafe_segments(tmp_path: Path, name: str) -> None:
    with pytest.raises(RpcClusterConfigError):
        resolve_ollama_gguf_blob(name, models_dir=tmp_path)


def test_ollama_manifest_symlink_escape_is_refused(tmp_path: Path) -> None:
    models_dir = tmp_path / "m"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "latest").write_text('{"layers": []}', encoding="utf-8")
    ns = models_dir / "manifests" / "registry.ollama.ai" / "library"
    ns.mkdir(parents=True)
    try:
        (ns / "demo").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert resolve_ollama_gguf_blob("demo:latest", models_dir=models_dir) is None


def test_default_http_get_does_not_follow_redirects() -> None:
    import http.server
    import threading

    from sonder_runtime.adapters.inference import rpc_cluster as rc

    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            hits.append(self.path)
            if self.path == "/health":
                self.send_response(302)
                self.send_header("Location", "/r/health")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            return None

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _ = rc._default_http_get(f"http://127.0.0.1:{server.server_port}/health", 2.0)
    finally:
        server.shutdown()
        server.server_close()
    assert status == 302
    assert hits == ["/health"]


@pytest.mark.parametrize(
    "url",
    ["http://user:pw@127.0.0.1:11438", "http://127.0.0.1:11438/?x=1", "http://127.0.0.1:11438/#f"],
)
def test_sharded_server_url_rejects_userinfo_query_fragment(url: str) -> None:
    with pytest.raises(RpcClusterConfigError):
        probe_sharded_server(url, get_fn=_fake_get)
