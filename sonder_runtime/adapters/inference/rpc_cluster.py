"""Distributed llama.cpp RPC sharding configuration, discovery, and health helpers.

Models are sharded across Main (CUDA/Vulkan ``llama-server --rpc``) and Node1
(Vulkan ``rpc-server``) exclusively over the ``10.77.0.0/30`` private link
(Main ``10.77.0.1`` <-> Node1 ``10.77.0.2:50052``).  The OpenAI-compatible HTTP
endpoint on Main binds strictly to loopback (``127.0.0.1:11438`` by default) so
Sonder's ``openai-compatible`` gateway treats it as local without cloud egress.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

PRIVATE_LINK_CIDR = ipaddress.IPv4Network("10.77.0.0/30")
DEFAULT_MAIN_PRIVATE_IP = "10.77.0.1"
DEFAULT_NODE1_PRIVATE_IP = "10.77.0.2"
DEFAULT_RPC_PORT = 50052
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 11438
DEFAULT_MODEL_ALIAS = "sonder-rpc"
DEFAULT_CTX_SIZE = 8192
DEFAULT_GPU_LAYERS = 999
DEFAULT_KV_CACHE_TYPE = "q8_0"
DEFAULT_FLASH_ATTN = "on"
DEFAULT_RPC_INSTALL_DIR_MAIN = Path(r"C:\Users\Nathan\AppData\Local\sonder\llama-rpc")
DEFAULT_RPC_INSTALL_DIR_NODE1 = r"C:\NewBuild\llama-rpc"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_VALID_KV_TYPES = frozenset({"f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"})
_VALID_FLASH_ATTN = frozenset({"on", "off", "auto"})
_MAX_HEALTH_BYTES = 1_048_576


class RpcClusterConfigError(ValueError):
    """Raised when an RPC cluster address, port, or option fails policy checks."""


def _parse_ipv4(value: str, *, label: str) -> ipaddress.IPv4Address:
    raw = str(value or "").strip()
    if not raw:
        raise RpcClusterConfigError(f"{label} must not be empty")
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise RpcClusterConfigError(f"{label} must be an IPv4 literal, got {value!r}") from exc
    if not isinstance(addr, ipaddress.IPv4Address):
        raise RpcClusterConfigError(f"{label} must be an IPv4 address, got {value!r}")
    return addr


def validate_private_link_host(host: str, *, label: str = "RPC host") -> str:
    """Require an IPv4 address inside the 10.77.0.0/30 Main<->Node1 link."""
    addr = _parse_ipv4(host, label=label)
    if addr.is_unspecified:
        raise RpcClusterConfigError(
            f"{label} must never bind to an unspecified address ({addr})"
        )
    if addr not in PRIVATE_LINK_CIDR:
        raise RpcClusterConfigError(
            f"{label} {addr} is outside the required private link {PRIVATE_LINK_CIDR}"
        )
    if addr == PRIVATE_LINK_CIDR.network_address or addr == PRIVATE_LINK_CIDR.broadcast_address:
        raise RpcClusterConfigError(
            f"{label} {addr} cannot be the network or broadcast address of {PRIVATE_LINK_CIDR}"
        )
    return str(addr)


def validate_server_bind_host(host: str) -> str:
    """Require the OpenAI-compatible llama-server listener to stay on loopback."""
    raw = str(host or "").strip()
    if not raw:
        raise RpcClusterConfigError("llama-server bind host must not be empty")
    if raw.lower() == "localhost":
        return "127.0.0.1"
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise RpcClusterConfigError(
            f"llama-server bind host must be a loopback IP literal, got {host!r}"
        ) from exc
    if addr.is_unspecified or not addr.is_loopback:
        raise RpcClusterConfigError(
            f"llama-server bind host {host!r} must be loopback-only (never 0.0.0.0 or non-loopback)"
        )
    return str(addr)


def _validate_port(port: int, *, label: str) -> int:
    if isinstance(port, bool) or not isinstance(port, int):
        raise RpcClusterConfigError(f"{label} must be an integer, got {port!r}")
    if port < 1 or port > 65535:
        raise RpcClusterConfigError(f"{label} must be between 1 and 65535, got {port}")
    return port


@dataclass(frozen=True)
class RpcPeerEndpoint:
    """A single llama.cpp RPC worker endpoint on the private 10.77.0.0/30 link."""

    host: str = DEFAULT_NODE1_PRIVATE_IP
    port: int = DEFAULT_RPC_PORT

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "host",
            validate_private_link_host(self.host, label="RPC peer host"),
        )
        object.__setattr__(self, "port", _validate_port(self.port, label="RPC peer port"))

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @classmethod
    def parse(cls, raw: str) -> RpcPeerEndpoint:
        text = str(raw or "").strip()
        if not text:
            raise RpcClusterConfigError("RPC peer endpoint must not be empty")
        if ":" in text:
            host_part, port_part = text.rsplit(":", 1)
            try:
                port = int(port_part.strip())
            except ValueError as exc:
                raise RpcClusterConfigError(
                    f"invalid RPC peer port in {raw!r}"
                ) from exc
        else:
            host_part = text
            port = DEFAULT_RPC_PORT
        return cls(host=host_part.strip(), port=port)


def parse_rpc_peers(raw: str) -> tuple[RpcPeerEndpoint, ...]:
    """Parse a comma/semicolon-separated list of 10.77.0.0/30 RPC peer endpoints."""
    tokens = [
        part.strip()
        for part in str(raw or "").replace(";", ",").split(",")
        if part.strip()
    ]
    if not tokens:
        return ()
    seen: set[str] = set()
    peers: list[RpcPeerEndpoint] = []
    for token in tokens:
        peer = RpcPeerEndpoint.parse(token)
        if peer.address not in seen:
            seen.add(peer.address)
            peers.append(peer)
    return tuple(peers)


def validate_tensor_split(tensor_split: str) -> str:
    """Validate comma-separated positive numeric weights for ``--tensor-split``."""
    raw = str(tensor_split or "").strip()
    if not raw:
        return ""
    parts = [part.strip() for part in raw.split(",")]
    if any(not part for part in parts):
        raise RpcClusterConfigError(f"invalid empty weight in tensor_split={tensor_split!r}")
    weights: list[float] = []
    for part in parts:
        try:
            value = float(part)
        except ValueError as exc:
            raise RpcClusterConfigError(
                f"invalid weight {part!r} in tensor_split={tensor_split!r}"
            ) from exc
        if value < 0.0:
            raise RpcClusterConfigError(
                f"tensor_split weights must be non-negative, got {tensor_split!r}"
            )
        weights.append(value)
    if sum(weights) <= 0.0:
        raise RpcClusterConfigError(
            f"tensor_split must have a positive sum, got {tensor_split!r}"
        )
    return ",".join(parts)


@dataclass(frozen=True)
class RpcHardwareProfile:
    """Hardware-tuned sharding parameters for a Main + Node1 pairing."""

    name: str
    node1_device: str
    tensor_split: str
    ctx_size: int
    n_gpu_layers: int
    notes: str


def load_inference_config() -> dict:
    """Load configuration from inference_models.json."""
    config_path = Path(__file__).parent.parent.parent / "config" / "inference_models.json"
    with open(config_path, "r") as f:
        return json.load(f)

def load_hardware_profiles() -> dict[str, RpcHardwareProfile]:
    """Load hardware-tuned sharding parameters from configuration."""
    data = load_inference_config()
    profiles = {}
    for name, p in data.get("profiles", {}).items():
        profiles[name] = RpcHardwareProfile(
            name=name,
            node1_device=p["node1_device"],
            tensor_split=p["tensor_split"],
            ctx_size=p["ctx_size"],
            n_gpu_layers=p["n_gpu_layers"],
            notes=p["notes"]
        )
    return profiles


def current_780m_profile() -> RpcHardwareProfile:
    """Profile for current Node1 hardware (Ryzen 780M iGPU + 2 GiB UMA, 46 GB RAM)."""
    return load_hardware_profiles()["main-5070ti-plus-node1-780m"]


def rx_9060_xt_16gb_profile() -> RpcHardwareProfile:
    """Target profile once RX 9060 XT 16 GB is installed on Node1 & UMA lowered."""
    return load_hardware_profiles()["main-5070ti-plus-node1-9060xt-16gb"]


@dataclass(frozen=True)
class Node1RpcServerConfig:
    """Launch configuration for ``ggml-rpc-server`` on Node1 (10.77.0.2:50052)."""

    host: str = DEFAULT_NODE1_PRIVATE_IP
    port: int = DEFAULT_RPC_PORT
    device: str = ""
    threads: int = 0
    use_cache: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "host",
            validate_private_link_host(self.host, label="Node1 rpc-server bind host"),
        )
        object.__setattr__(self, "port", _validate_port(self.port, label="Node1 rpc-server port"))
        if self.threads < 0:
            raise RpcClusterConfigError(f"threads must be >= 0, got {self.threads}")

    @property
    def endpoint(self) -> RpcPeerEndpoint:
        return RpcPeerEndpoint(host=self.host, port=self.port)

    def build_args(self) -> list[str]:
        args = ["--host", self.host, "--port", str(self.port)]
        if self.device.strip():
            args.extend(["--device", self.device.strip()])
        if self.threads > 0:
            args.extend(["--threads", str(self.threads)])
        if self.use_cache:
            args.append("-c")
        return args


@dataclass(frozen=True)
class ShardedLlamaServerConfig:
    """Launch configuration for Main's ``llama-server --rpc ...`` endpoint."""

    model_path: str
    alias: str = DEFAULT_MODEL_ALIAS
    host: str = DEFAULT_SERVER_HOST
    port: int = DEFAULT_SERVER_PORT
    rpc_peers: tuple[RpcPeerEndpoint, ...] = field(
        default_factory=lambda: (RpcPeerEndpoint(),)
    )
    ctx_size: int = DEFAULT_CTX_SIZE
    n_gpu_layers: int = DEFAULT_GPU_LAYERS
    tensor_split: str = ""
    flash_attn: str = DEFAULT_FLASH_ATTN
    cache_type_k: str = DEFAULT_KV_CACHE_TYPE
    cache_type_v: str = DEFAULT_KV_CACHE_TYPE
    parallel: int = 1
    no_webui: bool = True
    extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.model_path or "").strip():
            raise RpcClusterConfigError("model_path must not be empty")
        if not str(self.alias or "").strip():
            raise RpcClusterConfigError("alias must not be empty")
        object.__setattr__(self, "host", validate_server_bind_host(self.host))
        object.__setattr__(self, "port", _validate_port(self.port, label="llama-server port"))
        if not self.rpc_peers:
            raise RpcClusterConfigError("at least one RPC peer is required")
        if self.ctx_size < 128:
            raise RpcClusterConfigError(f"ctx_size must be >= 128, got {self.ctx_size}")
        if self.n_gpu_layers < 0:
            raise RpcClusterConfigError(f"n_gpu_layers must be >= 0, got {self.n_gpu_layers}")
        if self.parallel < 1:
            raise RpcClusterConfigError(f"parallel must be >= 1, got {self.parallel}")
        object.__setattr__(self, "tensor_split", validate_tensor_split(self.tensor_split))
        fa = str(self.flash_attn or "").strip().lower()
        if fa not in _VALID_FLASH_ATTN:
            raise RpcClusterConfigError(
                f"flash_attn must be one of {sorted(_VALID_FLASH_ATTN)}, got {self.flash_attn!r}"
            )
        object.__setattr__(self, "flash_attn", fa)
        for field_name, kv_value in (("cache_type_k", self.cache_type_k), ("cache_type_v", self.cache_type_v)):
            normalized = str(kv_value or "").strip().lower()
            if normalized not in _VALID_KV_TYPES:
                raise RpcClusterConfigError(
                    f"{field_name} must be one of {sorted(_VALID_KV_TYPES)}, got {kv_value!r}"
                )
            object.__setattr__(self, field_name, normalized)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def build_args(self) -> list[str]:
        rpc_arg = ",".join(peer.address for peer in self.rpc_peers)
        args = [
            "--model",
            str(self.model_path).strip(),
            "--alias",
            str(self.alias).strip(),
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--rpc",
            rpc_arg,
            "--ctx-size",
            str(self.ctx_size),
            "--n-gpu-layers",
            str(self.n_gpu_layers),
            "--flash-attn",
            self.flash_attn,
            "--cache-type-k",
            self.cache_type_k,
            "--cache-type-v",
            self.cache_type_v,
            "--parallel",
            str(self.parallel),
        ]
        if self.tensor_split:
            args.extend(["--tensor-split", self.tensor_split])
        if self.no_webui:
            args.append("--no-webui")
        if self.extra_args:
            args.extend(str(item) for item in self.extra_args if str(item))
        return args


@dataclass(frozen=True)
class RpcPeerHealth:
    endpoint: str
    reachable: bool
    latency_ms: float | None = None
    error: str = ""


@dataclass(frozen=True)
class ShardedServerHealth:
    base_url: str
    healthy: bool
    status: str = ""
    model_ids: tuple[str, ...] = ()
    latency_ms: float | None = None
    error: str = ""


TcpConnectProbe = Callable[[tuple[str, int], float], None]
HttpGetProbe = Callable[[str, float], tuple[int, bytes]]


def _default_tcp_connect(address: tuple[str, int], timeout: float) -> None:
    with socket.create_connection(address, timeout=timeout):
        return None


def _default_http_get(url: str, timeout: float) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            body = response.read(_MAX_HEALTH_BYTES)
            return status, body
    except urllib.error.HTTPError as exc:
        body = exc.read(_MAX_HEALTH_BYTES)
        return int(exc.code), body


def probe_rpc_peer(
    peer: RpcPeerEndpoint | str,
    *,
    timeout: float = 2.0,
    connect_fn: TcpConnectProbe | None = None,
) -> RpcPeerHealth:
    """Check whether a 10.77.0.0/30 Node1 RPC worker TCP socket is accepting connections."""
    resolved = peer if isinstance(peer, RpcPeerEndpoint) else RpcPeerEndpoint.parse(peer)
    connector = _default_tcp_connect if connect_fn is None else connect_fn
    started = time.perf_counter()
    try:
        connector((resolved.host, resolved.port), float(timeout))
    except OSError as exc:
        return RpcPeerHealth(
            endpoint=resolved.address,
            reachable=False,
            latency_ms=round((time.perf_counter() - started) * 1000.0, 2),
            error=str(exc) or type(exc).__name__,
        )
    return RpcPeerHealth(
        endpoint=resolved.address,
        reachable=True,
        latency_ms=round((time.perf_counter() - started) * 1000.0, 2),
    )


def _validate_loopback_base_url(base_url: str) -> str:
    raw = str(base_url or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RpcClusterConfigError(f"invalid sharded server URL: {base_url!r}")
    if parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        raise RpcClusterConfigError(
            f"sharded llama-server URL must be loopback-only, got {base_url!r}"
        )
    return raw


def probe_sharded_server(
    base_url: str = f"http://{DEFAULT_SERVER_HOST}:{DEFAULT_SERVER_PORT}",
    *,
    timeout: float = 3.0,
    get_fn: HttpGetProbe | None = None,
) -> ShardedServerHealth:
    """Probe Main's local OpenAI-compatible sharded llama-server (/health + /v1/models)."""
    clean_url = _validate_loopback_base_url(base_url)
    getter = _default_http_get if get_fn is None else get_fn
    started = time.perf_counter()
    try:
        health_status, health_body = getter(f"{clean_url}/health", float(timeout))
        health_payload = json.loads(health_body.decode("utf-8"))
    except (OSError, ValueError) as exc:
        return ShardedServerHealth(
            base_url=clean_url,
            healthy=False,
            latency_ms=round((time.perf_counter() - started) * 1000.0, 2),
            error=str(exc) or type(exc).__name__,
        )
    if not isinstance(health_payload, dict):
        return ShardedServerHealth(
            base_url=clean_url,
            healthy=False,
            latency_ms=round((time.perf_counter() - started) * 1000.0, 2),
            error="unexpected /health JSON shape",
        )
    status_text = str(health_payload.get("status", "") or "")
    if health_status != 200 or status_text != "ok":
        return ShardedServerHealth(
            base_url=clean_url,
            healthy=False,
            status=status_text or f"http_{health_status}",
            latency_ms=round((time.perf_counter() - started) * 1000.0, 2),
            error=f"/health returned status={health_status} body_status={status_text!r}",
        )

    model_ids: tuple[str, ...] = ()
    try:
        models_status, models_body = getter(f"{clean_url}/v1/models", float(timeout))
        if models_status == 200:
            models_payload = json.loads(models_body.decode("utf-8"))
            if isinstance(models_payload, dict) and isinstance(models_payload.get("data"), list):
                model_ids = tuple(
                    str(item["id"])
                    for item in models_payload["data"]
                    if isinstance(item, dict) and item.get("id")
                )
    except (OSError, ValueError):
        model_ids = ()

    return ShardedServerHealth(
        base_url=clean_url,
        healthy=True,
        status=status_text,
        model_ids=model_ids,
        latency_ms=round((time.perf_counter() - started) * 1000.0, 2),
    )


def discover_rpc_binaries(install_dir: Path | None = None) -> dict[str, Path | None]:
    """Locate ``llama-server.exe`` and ``rpc-server.exe`` inside the dedicated RPC folder."""
    root = DEFAULT_RPC_INSTALL_DIR_MAIN if install_dir is None else Path(install_dir)
    llama_server = root / "llama-server.exe"
    rpc_server_candidates = (root / "rpc-server.exe", root / "ggml-rpc-server.exe")
    rpc_server = next((p for p in rpc_server_candidates if p.is_file()), None)
    return {
        "install_dir": root if root.is_dir() else None,
        "llama_server": llama_server if llama_server.is_file() else None,
        "rpc_server": rpc_server,
    }


def _parse_ollama_model_ref(model_name: str) -> tuple[str, str, str]:
    raw = str(model_name or "").strip()
    if not raw:
        raise RpcClusterConfigError("Ollama model name must not be empty")
    if ":" in raw:
        repo_part, tag = raw.rsplit(":", 1)
    else:
        repo_part, tag = raw, "latest"
    segments = [seg for seg in repo_part.split("/") if seg]
    if len(segments) == 1:
        return "registry.ollama.ai", "library", f"{segments[0]}/{tag}"
    if len(segments) == 2:
        return "registry.ollama.ai", segments[0], f"{segments[1]}/{tag}"
    return segments[0], "/".join(segments[1:-1]), f"{segments[-1]}/{tag}"


def resolve_ollama_gguf_blob(
    model_name: str,
    *,
    models_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """Resolve an already-pulled Ollama model tag to its on-disk GGUF blob path."""
    source_env = os.environ if env is None else env
    if models_dir is not None:
        base_dir = Path(models_dir)
    else:
        configured = str(source_env.get("OLLAMA_MODELS", "") or "").strip()
        base_dir = Path(configured) if configured else (Path.home() / ".ollama" / "models")
    registry, namespace, model_and_tag = _parse_ollama_model_ref(model_name)
    model_part, tag_part = model_and_tag.rsplit("/", 1)
    manifest_path = base_dir / "manifests" / registry / namespace / model_part / tag_part
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    layers = manifest.get("layers")
    if not isinstance(layers, list):
        return None
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        if layer.get("mediaType") != "application/vnd.ollama.image.model":
            continue
        digest = str(layer.get("digest", "") or "").strip()
        if not digest.startswith("sha256:"):
            continue
        blob_path = base_dir / "blobs" / digest.replace(":", "-", 1)
        if blob_path.is_file():
            return blob_path
    return None


def sonder_openai_env_for_rpc(
    base_url: str = f"http://{DEFAULT_SERVER_HOST}:{DEFAULT_SERVER_PORT}",
    *,
    model_alias: str = DEFAULT_MODEL_ALIAS,
    tiers: Sequence[str] = ("general", "reasoning"),
    rpc_peers: Sequence[RpcPeerEndpoint | str] = (DEFAULT_NODE1_PRIVATE_IP,),
) -> dict[str, str]:
    """Build Sonder env bindings wiring selected tiers to the sharded llama-server."""
    clean_url = _validate_loopback_base_url(base_url)
    clean_alias = str(model_alias or "").strip()
    if not clean_alias:
        raise RpcClusterConfigError("model_alias must not be empty")
    tier_env_map = {
        "fast": "SONDER_FAST_PROVIDER",
        "general": "SONDER_GENERAL_PROVIDER",
        "code": "SONDER_CODE_PROVIDER",
        "reasoning": "SONDER_REASONING_PROVIDER",
        "vision": "SONDER_VISION_PROVIDER",
    }
    validated_peers = tuple(
        peer if isinstance(peer, RpcPeerEndpoint) else RpcPeerEndpoint.parse(peer)
        for peer in rpc_peers
    )
    if not validated_peers:
        raise RpcClusterConfigError("at least one RPC peer is required")
    env_vars: dict[str, str] = {
        "SONDER_OPENAI_BASE_URL": clean_url,
        "SONDER_OPENAI_MODEL": clean_alias,
        "SONDER_RPC_PEERS": ",".join(peer.address for peer in validated_peers),
    }
    for tier in tiers:
        normalized = str(tier or "").strip().lower()
        if normalized not in tier_env_map:
            raise RpcClusterConfigError(f"unknown Sonder tier {tier!r}")
        env_vars[tier_env_map[normalized]] = "openai-compatible"
    return env_vars


__all__ = [
    "DEFAULT_CTX_SIZE",
    "DEFAULT_FLASH_ATTN",
    "DEFAULT_GPU_LAYERS",
    "DEFAULT_KV_CACHE_TYPE",
    "DEFAULT_MAIN_PRIVATE_IP",
    "DEFAULT_MODEL_ALIAS",
    "DEFAULT_NODE1_PRIVATE_IP",
    "DEFAULT_RPC_INSTALL_DIR_MAIN",
    "DEFAULT_RPC_INSTALL_DIR_NODE1",
    "DEFAULT_RPC_PORT",
    "DEFAULT_SERVER_HOST",
    "DEFAULT_SERVER_PORT",
    "Node1RpcServerConfig",
    "PRIVATE_LINK_CIDR",
    "RpcClusterConfigError",
    "RpcHardwareProfile",
    "RpcPeerEndpoint",
    "RpcPeerHealth",
    "ShardedLlamaServerConfig",
    "ShardedServerHealth",
    "current_780m_profile",
    "discover_rpc_binaries",
    "parse_rpc_peers",
    "probe_rpc_peer",
    "probe_sharded_server",
    "resolve_ollama_gguf_blob",
    "rx_9060_xt_16gb_profile",
    "sonder_openai_env_for_rpc",
    "validate_private_link_host",
    "validate_server_bind_host",
    "validate_tensor_split",
]
