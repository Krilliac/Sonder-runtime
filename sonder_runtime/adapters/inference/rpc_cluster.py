"""Opt-in llama.cpp RPC sharding: launch configuration, discovery and health helpers.

One model is split across the main machine's GPU (``llama-server --rpc``) and
one or more workers that run llama.cpp ``rpc-server`` on a private link. The
feature is off unless ``SONDER_RPC_PEERS`` names at least one worker; the
allowlist and port policy lives in ``sonder_runtime.platform.rpc_sharding_config``
and is applied here and at configuration load alike.

The OpenAI-compatible ``llama-server`` listener on the main machine binds to
loopback only (``127.0.0.1:11438`` by default), so Sonder's
``openai-compatible`` gateway reaches it as a local endpoint. Worker addresses,
install directories and profiles come from configuration, the environment or
explicit arguments; nothing here names a particular machine.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sonder_runtime.adapters.provider_bindings import TIER_PROVIDER_ENV
from sonder_runtime.platform.rpc_sharding_config import (
    DEFAULT_RPC_PORT,
    MAX_RPC_PEERS,
    RPC_PEERS_ENV,
    RPC_PORT_ENV,
    RpcShardingConfigError,
    parse_peer_allowlist,
    parse_peer_entry,
    rpc_peer_allowlist,
    validate_peer_host,
    validate_rpc_port,
)

# One exception type for the whole feature: the platform policy and these
# helpers raise the same class, so callers catch a single error.
RpcClusterConfigError = RpcShardingConfigError

_logger = logging.getLogger("sonder.rpc_sharding")

OPENAI_BASE_URL_ENV = "SONDER_OPENAI_BASE_URL"
RPC_INSTALL_DIR_ENV = "SONDER_RPC_INSTALL_DIR"
RPC_PROFILES_FILE_ENV = "SONDER_RPC_PROFILES_FILE"
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 11438
DEFAULT_MODEL_ALIAS = "sonder-rpc"
DEFAULT_CTX_SIZE = 8192
DEFAULT_GPU_LAYERS = 999
DEFAULT_KV_CACHE_TYPE = "q8_0"
DEFAULT_FLASH_ATTN = "on"
DEFAULT_PROFILES_PATH = Path(__file__).resolve().parents[1] / "data" / "rpc_sharding_profiles.json"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_VALID_KV_TYPES = frozenset({"f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"})
_VALID_FLASH_ATTN = frozenset({"on", "off", "auto"})
# Options the launcher owns. Letting extra_args repeat them could move the
# listener off loopback or point --rpc at an address the allowlist refused.
_RESERVED_SERVER_OPTIONS = frozenset({"--host", "--port", "--rpc"})
_MAX_HEALTH_BYTES = 1_048_576
_MAX_PROFILES_BYTES = 262_144
_PROFILE_FIELDS = frozenset({"worker_device", "tensor_split", "ctx_size", "n_gpu_layers", "notes"})


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


def _validate_int(value: object, *, label: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RpcClusterConfigError(f"{label} must be an integer, got {value!r}")
    if value < minimum:
        raise RpcClusterConfigError(f"{label} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class RpcPeerEndpoint:
    """One allowlisted llama.cpp RPC worker: a private or link-local IPv4 literal and port."""

    host: str
    port: int = DEFAULT_RPC_PORT

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", validate_peer_host(self.host, label="RPC peer host"))
        object.__setattr__(self, "port", validate_rpc_port(self.port, label="RPC peer port"))

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @classmethod
    def parse(cls, raw: str, *, port: int = DEFAULT_RPC_PORT) -> RpcPeerEndpoint:
        """Parse ``ip`` or ``ip:port``; an explicit port must equal ``port``."""
        host, peer_port = parse_peer_entry(str(raw or ""), port=port)
        return cls(host=host, port=peer_port)


def parse_rpc_peers(raw: str, *, port: int = DEFAULT_RPC_PORT) -> tuple[RpcPeerEndpoint, ...]:
    """Parse a comma/semicolon-separated worker allowlist that must all use ``port``."""
    return tuple(
        RpcPeerEndpoint(host=host, port=peer_port)
        for host, peer_port in parse_peer_allowlist(raw, port=port)
    )


def _coerce_peers(
    peers: Sequence[RpcPeerEndpoint | str], *, port: int
) -> tuple[RpcPeerEndpoint, ...]:
    resolved: list[RpcPeerEndpoint] = []
    for peer in peers:
        endpoint = peer if isinstance(peer, RpcPeerEndpoint) else RpcPeerEndpoint.parse(peer, port=port)
        if endpoint.port != port:
            raise RpcClusterConfigError(
                f"RPC peer {endpoint.address} does not use the configured RPC port {port}"
            )
        if endpoint not in resolved:
            resolved.append(endpoint)
    if not resolved:
        raise RpcClusterConfigError("at least one RPC peer is required")
    if len(resolved) > MAX_RPC_PEERS:
        raise RpcClusterConfigError(f"at most {MAX_RPC_PEERS} RPC peers are allowed")
    return tuple(resolved)


def _validate_loopback_base_url(base_url: str) -> str:
    raw = str(base_url or "").strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RpcClusterConfigError(f"invalid sharded server URL: {base_url!r}") from exc
    if port is not None and not 1 <= port <= 65535:
        raise RpcClusterConfigError(f"invalid sharded server URL port: {base_url!r}")
    if parsed.scheme not in ("http", "https") or not hostname:
        raise RpcClusterConfigError(f"invalid sharded server URL: {base_url!r}")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise RpcClusterConfigError("sharded server URL must not contain credentials")
    if parsed.query or parsed.fragment or "?" in raw or "#" in raw:
        raise RpcClusterConfigError("sharded server URL must not contain a query or fragment")
    if hostname.lower() not in _LOOPBACK_HOSTS:
        raise RpcClusterConfigError(
            f"sharded llama-server URL must be loopback-only, got {base_url!r}"
        )
    return raw


@dataclass(frozen=True)
class RpcShardingSettings:
    """The active sharding configuration: allowlisted workers and the loopback server URL."""

    peers: tuple[RpcPeerEndpoint, ...]
    rpc_port: int
    server_url: str


def rpc_sharding_settings_from_env(
    env: Mapping[str, str] | None = None,
) -> RpcShardingSettings | None:
    """Return the sharding settings, or ``None`` when ``SONDER_RPC_PEERS`` is unset or blank.

    Raises ``RpcClusterConfigError`` when peers are configured but any entry,
    the port, or ``SONDER_OPENAI_BASE_URL`` fails the policy.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    allowlist = rpc_peer_allowlist(source)
    if not allowlist.active:
        return None
    base_url = str(source.get(OPENAI_BASE_URL_ENV, "") or "").strip()
    if not base_url:
        raise RpcClusterConfigError(
            f"{RPC_PEERS_ENV} is set, so {OPENAI_BASE_URL_ENV} must name the loopback "
            "sharded llama-server"
        )
    return RpcShardingSettings(
        peers=tuple(RpcPeerEndpoint(host=host, port=port) for host, port in allowlist.peers),
        rpc_port=allowlist.port,
        server_url=_validate_loopback_base_url(base_url),
    )


def validate_tensor_split(tensor_split: str) -> str:
    """Validate comma-separated non-negative numeric weights for ``--tensor-split``."""
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
        if not value >= 0.0 or value == float("inf"):
            raise RpcClusterConfigError(
                f"tensor_split weights must be finite and non-negative, got {tensor_split!r}"
            )
        weights.append(value)
    if sum(weights) <= 0.0:
        raise RpcClusterConfigError(
            f"tensor_split must have a positive sum, got {tensor_split!r}"
        )
    return ",".join(parts)


@dataclass(frozen=True)
class RpcHardwareProfile:
    """Named sharding parameters for a main GPU plus worker pairing."""

    name: str
    worker_device: str
    tensor_split: str
    ctx_size: int
    n_gpu_layers: int
    notes: str


def _profile_from_mapping(name: str, raw: object) -> RpcHardwareProfile:
    label = f"RPC profile {name!r}"
    if not isinstance(raw, dict):
        raise RpcClusterConfigError(f"{label} must be a JSON object")
    keys = set(raw)
    unknown = sorted(str(key) for key in keys - _PROFILE_FIELDS)
    missing = sorted(_PROFILE_FIELDS - keys)
    if unknown or missing:
        raise RpcClusterConfigError(
            f"{label} has unknown fields {unknown} or is missing fields {missing}"
        )
    worker_device = raw["worker_device"]
    tensor_split = raw["tensor_split"]
    notes = raw["notes"]
    if not isinstance(worker_device, str) or not isinstance(tensor_split, str) or not isinstance(notes, str):
        raise RpcClusterConfigError(f"{label}: worker_device, tensor_split and notes must be strings")
    return RpcHardwareProfile(
        name=name,
        worker_device=worker_device.strip(),
        tensor_split=validate_tensor_split(tensor_split),
        ctx_size=_validate_int(raw["ctx_size"], label=f"{label} ctx_size", minimum=128),
        n_gpu_layers=_validate_int(raw["n_gpu_layers"], label=f"{label} n_gpu_layers", minimum=0),
        notes=notes,
    )


def default_profiles_path(env: Mapping[str, str] | None = None) -> Path:
    """``SONDER_RPC_PROFILES_FILE`` when set, else the profiles shipped with the runtime."""
    source: Mapping[str, str] = os.environ if env is None else env
    configured = str(source.get(RPC_PROFILES_FILE_ENV, "") or "").strip()
    return Path(configured) if configured else DEFAULT_PROFILES_PATH


def load_hardware_profiles(
    path: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, RpcHardwareProfile]:
    """Load and validate named sharding profiles from a JSON ``{"profiles": {...}}`` file."""
    profiles_path = default_profiles_path(env) if path is None else Path(path)
    try:
        with profiles_path.open("rb") as handle:
            data = handle.read(_MAX_PROFILES_BYTES + 1)
    except OSError as exc:
        raise RpcClusterConfigError(
            f"RPC profiles file unreadable: {profiles_path} ({exc.strerror or type(exc).__name__})"
        ) from exc
    if len(data) > _MAX_PROFILES_BYTES:
        raise RpcClusterConfigError(f"RPC profiles file exceeds {_MAX_PROFILES_BYTES} bytes: {profiles_path}")
    try:
        document = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise RpcClusterConfigError(f"RPC profiles file is not valid UTF-8 JSON: {profiles_path}") from exc
    if not isinstance(document, dict) or set(document) != {"profiles"}:
        raise RpcClusterConfigError(
            f"RPC profiles file must be an object with exactly one key 'profiles': {profiles_path}"
        )
    entries = document["profiles"]
    if not isinstance(entries, dict):
        raise RpcClusterConfigError(f"'profiles' must be a JSON object: {profiles_path}")
    profiles: dict[str, RpcHardwareProfile] = {}
    for name, raw in entries.items():
        clean_name = str(name).strip()
        if not clean_name:
            raise RpcClusterConfigError(f"RPC profile names must not be empty: {profiles_path}")
        profiles[clean_name] = _profile_from_mapping(clean_name, raw)
    return profiles


def get_hardware_profile(
    name: str,
    *,
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> RpcHardwareProfile:
    """Return one named profile; unknown names raise ``RpcClusterConfigError``."""
    profiles = load_hardware_profiles(path, env=env)
    try:
        return profiles[str(name).strip()]
    except KeyError as exc:
        raise RpcClusterConfigError(
            f"unknown RPC profile {name!r}; known profiles: {sorted(profiles)}"
        ) from exc


@dataclass(frozen=True)
class RpcWorkerServerConfig:
    """Launch configuration for llama.cpp ``rpc-server`` on a worker's private-link address."""

    host: str
    port: int = DEFAULT_RPC_PORT
    device: str = ""
    threads: int = 0
    use_cache: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "host",
            validate_peer_host(self.host, label="rpc-server bind host"),
        )
        object.__setattr__(self, "port", validate_rpc_port(self.port, label="rpc-server port"))
        _validate_int(self.threads, label="threads", minimum=0)

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
    """Launch configuration for the main machine's ``llama-server --rpc ...`` endpoint."""

    model_path: str
    rpc_peers: tuple[RpcPeerEndpoint, ...]
    alias: str = DEFAULT_MODEL_ALIAS
    host: str = DEFAULT_SERVER_HOST
    port: int = DEFAULT_SERVER_PORT
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
        object.__setattr__(self, "port", validate_rpc_port(self.port, label="llama-server port"))
        peers = tuple(self.rpc_peers)
        if not peers:
            raise RpcClusterConfigError("at least one RPC peer is required")
        if not all(isinstance(peer, RpcPeerEndpoint) for peer in peers):
            raise RpcClusterConfigError("rpc_peers must contain RpcPeerEndpoint values")
        if len({peer.port for peer in peers}) != 1:
            raise RpcClusterConfigError("every RPC peer must use the one configured RPC port")
        object.__setattr__(self, "rpc_peers", _coerce_peers(peers, port=peers[0].port))
        _validate_int(self.ctx_size, label="ctx_size", minimum=128)
        _validate_int(self.n_gpu_layers, label="n_gpu_layers", minimum=0)
        _validate_int(self.parallel, label="parallel", minimum=1)
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
        extras = tuple(str(item) for item in self.extra_args if str(item))
        for item in extras:
            option = item.split("=", 1)[0].strip().lower()
            if option in _RESERVED_SERVER_OPTIONS:
                raise RpcClusterConfigError(
                    f"extra_args must not set {option}; host, port and RPC peers are validated fields"
                )
        object.__setattr__(self, "extra_args", extras)

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

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
        args.extend(self.extra_args)
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the loopback probe can never be steered off-host."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _default_http_get(url: str, timeout: float) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(url, method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            body: bytes = response.read(_MAX_HEALTH_BYTES)
            return status, body
    except urllib.error.HTTPError as exc:
        error_body: bytes = exc.read(_MAX_HEALTH_BYTES)
        return int(exc.code), error_body


def probe_rpc_peer(
    peer: RpcPeerEndpoint | str,
    *,
    port: int = DEFAULT_RPC_PORT,
    timeout: float = 2.0,
    connect_fn: TcpConnectProbe | None = None,
) -> RpcPeerHealth:
    """Check whether an allowlisted RPC worker's TCP socket accepts connections."""
    resolved = peer if isinstance(peer, RpcPeerEndpoint) else RpcPeerEndpoint.parse(peer, port=port)
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


def probe_sharded_server(
    base_url: str = f"http://{DEFAULT_SERVER_HOST}:{DEFAULT_SERVER_PORT}",
    *,
    timeout: float = 3.0,
    get_fn: HttpGetProbe | None = None,
) -> ShardedServerHealth:
    """Probe the main machine's loopback sharded llama-server (/health + /v1/models)."""
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


def probe_configured_sharded_server(
    env: Mapping[str, str] | None = None,
    *,
    timeout: float = 3.0,
    get_fn: HttpGetProbe | None = None,
) -> ShardedServerHealth | None:
    """Probe the configured sharded server; ``None`` when sharding is not configured."""
    settings = rpc_sharding_settings_from_env(env)
    if settings is None:
        return None
    return probe_sharded_server(settings.server_url, timeout=timeout, get_fn=get_fn)


def run_sharded_backend_probe(
    env: Mapping[str, str],
    *,
    get_fn: HttpGetProbe | None = None,
) -> ShardedServerHealth | None:
    """Probe the configured sharded server once and log one line. Never raises."""
    try:
        health = probe_configured_sharded_server(env, get_fn=get_fn)
    except Exception as exc:
        _logger.warning("RPC sharded backend probe failed (%s)", type(exc).__name__)
        return None
    if health is None:
        return None
    _logger.log(
        logging.INFO if health.healthy else logging.WARNING,
        "RPC sharded backend: healthy=%s status=%s models=%s error=%s",
        health.healthy,
        health.status or "-",
        list(health.model_ids),
        health.error or "-",
    )
    return health


def start_sharded_backend_probe(
    *,
    thread_factory: Callable[..., Any],
    env: Mapping[str, str] | None = None,
) -> bool:
    """Start one background readiness probe when sharding is configured.

    Called by ``serve`` right after its listener is bound. The environment is
    copied here, on the caller's thread; without ``SONDER_RPC_PEERS`` no thread
    is created. Returns ``True`` when the probe thread started. Never raises.
    """
    snapshot = dict(os.environ if env is None else env)
    if not str(snapshot.get(RPC_PEERS_ENV, "") or "").strip():
        return False
    try:
        thread = thread_factory(
            target=run_sharded_backend_probe, args=(snapshot,),
            name="sonder-rpc-probe", daemon=True,
        )
        thread.start()
    except Exception:
        _logger.warning("could not start the RPC sharded backend probe thread", exc_info=True)
        return False
    return True


def default_rpc_install_dir(env: Mapping[str, str] | None = None) -> Path:
    """``SONDER_RPC_INSTALL_DIR``, else ``%LOCALAPPDATA%\\sonder\\llama-rpc``, else ``~/.local/share``."""
    source: Mapping[str, str] = os.environ if env is None else env
    configured = str(source.get(RPC_INSTALL_DIR_ENV, "") or "").strip()
    if configured:
        return Path(configured)
    local_app_data = str(source.get("LOCALAPPDATA", "") or "").strip()
    if local_app_data:
        return Path(local_app_data) / "sonder" / "llama-rpc"
    return Path.home() / ".local" / "share" / "sonder" / "llama-rpc"


def discover_rpc_binaries(
    install_dir: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Path | None]:
    """Locate ``llama-server`` and ``rpc-server`` inside the dedicated RPC folder."""
    root = default_rpc_install_dir(env) if install_dir is None else Path(install_dir)
    server_candidates = (root / "llama-server.exe", root / "llama-server")
    rpc_server_candidates = (
        root / "rpc-server.exe",
        root / "ggml-rpc-server.exe",
        root / "rpc-server",
        root / "ggml-rpc-server",
    )
    return {
        "install_dir": root if root.is_dir() else None,
        "llama_server": next((p for p in server_candidates if p.is_file()), None),
        "rpc_server": next((p for p in rpc_server_candidates if p.is_file()), None),
    }


_OLLAMA_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _parse_ollama_model_ref(model_name: str) -> tuple[str, str, str]:
    raw = str(model_name or "").strip()
    if not raw:
        raise RpcClusterConfigError("Ollama model name must not be empty")
    if ":" in raw:
        repo_part, tag = raw.rsplit(":", 1)
    else:
        repo_part, tag = raw, "latest"
    segments = [seg for seg in repo_part.split("/") if seg]
    if not segments or not tag or not all(
        _OLLAMA_SEGMENT_RE.fullmatch(seg) for seg in (*segments, tag)
    ):
        raise RpcClusterConfigError(f"invalid Ollama model name {model_name!r}")
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
    source_env: Mapping[str, str] = os.environ if env is None else env
    if models_dir is not None:
        base_dir = Path(models_dir)
    else:
        configured = str(source_env.get("OLLAMA_MODELS", "") or "").strip()
        base_dir = Path(configured) if configured else (Path.home() / ".ollama" / "models")
    registry, namespace, model_and_tag = _parse_ollama_model_ref(model_name)
    model_part, tag_part = model_and_tag.rsplit("/", 1)
    manifest_path = base_dir / "manifests" / registry / namespace / model_part / tag_part
    try:
        manifests_root = (base_dir / "manifests").resolve()
        if not manifest_path.resolve().is_relative_to(manifests_root):
            return None
    except (OSError, ValueError):
        return None
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
        hex_part = digest[len("sha256:"):]
        if not digest.startswith("sha256:") or not hex_part or not all(
            ch in "0123456789abcdef" for ch in hex_part
        ):
            continue
        blob_path = base_dir / "blobs" / f"sha256-{hex_part}"
        if blob_path.is_file():
            return blob_path
    return None


def sonder_openai_env_for_rpc(
    base_url: str = f"http://{DEFAULT_SERVER_HOST}:{DEFAULT_SERVER_PORT}",
    *,
    rpc_peers: Sequence[RpcPeerEndpoint | str],
    rpc_port: int = DEFAULT_RPC_PORT,
    model_alias: str = DEFAULT_MODEL_ALIAS,
    tiers: Sequence[str] = ("general", "reasoning"),
) -> dict[str, str]:
    """Build Sonder env bindings wiring selected tiers to the sharded llama-server."""
    clean_url = _validate_loopback_base_url(base_url)
    clean_alias = str(model_alias or "").strip()
    if not clean_alias:
        raise RpcClusterConfigError("model_alias must not be empty")
    port = validate_rpc_port(rpc_port, label=RPC_PORT_ENV)
    validated_peers = _coerce_peers(rpc_peers, port=port)
    env_vars: dict[str, str] = {
        OPENAI_BASE_URL_ENV: clean_url,
        "SONDER_OPENAI_MODEL": clean_alias,
        RPC_PEERS_ENV: ",".join(peer.address for peer in validated_peers),
        RPC_PORT_ENV: str(port),
    }
    for tier in tiers:
        normalized = str(tier or "").strip().lower()
        if normalized not in TIER_PROVIDER_ENV:
            raise RpcClusterConfigError(f"unknown Sonder tier {tier!r}")
        env_vars[TIER_PROVIDER_ENV[normalized]] = "openai-compatible"
    return env_vars


__all__ = [
    "DEFAULT_CTX_SIZE",
    "DEFAULT_FLASH_ATTN",
    "DEFAULT_GPU_LAYERS",
    "DEFAULT_KV_CACHE_TYPE",
    "DEFAULT_MODEL_ALIAS",
    "DEFAULT_PROFILES_PATH",
    "DEFAULT_RPC_PORT",
    "DEFAULT_SERVER_HOST",
    "DEFAULT_SERVER_PORT",
    "OPENAI_BASE_URL_ENV",
    "RPC_INSTALL_DIR_ENV",
    "RPC_PEERS_ENV",
    "RPC_PORT_ENV",
    "RPC_PROFILES_FILE_ENV",
    "RpcClusterConfigError",
    "RpcHardwareProfile",
    "RpcPeerEndpoint",
    "RpcPeerHealth",
    "RpcShardingSettings",
    "RpcWorkerServerConfig",
    "ShardedLlamaServerConfig",
    "ShardedServerHealth",
    "default_profiles_path",
    "default_rpc_install_dir",
    "discover_rpc_binaries",
    "get_hardware_profile",
    "load_hardware_profiles",
    "parse_rpc_peers",
    "probe_configured_sharded_server",
    "probe_rpc_peer",
    "probe_sharded_server",
    "resolve_ollama_gguf_blob",
    "rpc_sharding_settings_from_env",
    "run_sharded_backend_probe",
    "sonder_openai_env_for_rpc",
    "start_sharded_backend_probe",
    "validate_peer_host",
    "validate_server_bind_host",
    "validate_tensor_split",
]
