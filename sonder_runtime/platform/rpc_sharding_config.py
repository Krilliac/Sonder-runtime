"""Opt-in llama.cpp RPC sharding: the worker allowlist, its port and activation.

Cross-PC sharding runs one model across the main machine's GPU and one or more
workers that run llama.cpp ``rpc-server`` on a private link. It is off unless
``SONDER_RPC_PEERS`` names at least one worker; unset or blank means inactive,
and nothing else (no default, no discovery) ever supplies a peer.

Every allowlist entry must be a literal IPv4 address inside an RFC 1918 private
range (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16) or the IPv4 link-local range
(169.254.0.0/16). Hostnames, IPv6, loopback, the unspecified address and every
public, documentation, multicast or reserved address are refused. Every entry
uses the one configured RPC port (``SONDER_RPC_PORT``, default 50052): an entry
without a port gets it, and an entry that names a different port is refused.

Two optional settings tighten the policy further. ``SONDER_RPC_LINK_CIDR`` names
the private link itself (an IPv4 network inside one of the ranges above, for
example ``10.77.0.0/30``); when set, every peer must sit inside it, so an
address that is private but off the link (a LAN or VPN address) is refused.
``SONDER_RPC_PROTO_MAJOR`` pins the llama.cpp RPC protocol major version
(``RPC_PROTO_MAJOR_VERSION`` in ``ggml-rpc.h``) that every worker must speak;
``adapters.inference.rpc_cluster`` checks it against each worker's HELLO reply
before a cross-host split is enabled.

The rules live in the platform layer so configuration validation
(``platform.config``) and the launch and probe helpers
(``adapters.inference.rpc_cluster``) apply one policy. Values are read from the
mapping the caller passes; this module never reads ``os.environ`` itself.
"""
from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass

RPC_PEERS_ENV = "SONDER_RPC_PEERS"
RPC_PORT_ENV = "SONDER_RPC_PORT"
RPC_LINK_CIDR_ENV = "SONDER_RPC_LINK_CIDR"
RPC_PROTO_MAJOR_ENV = "SONDER_RPC_PROTO_MAJOR"
DEFAULT_RPC_PORT = 50052
MAX_RPC_PEERS = 16
# The RPC HELLO reply carries the major version in one byte.
MAX_RPC_PROTO_MAJOR = 255

ALLOWED_PEER_NETWORKS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),
)


class RpcShardingConfigError(ValueError):
    """An RPC sharding address, port or option failed the fail-closed policy."""


def validate_peer_host(value: object, *, label: str = "RPC peer host") -> str:
    """Return ``value`` as a canonical IPv4 literal on a private or link-local range."""
    raw = value.strip() if isinstance(value, str) else ""
    if not raw:
        raise RpcShardingConfigError(f"{label} must not be empty")
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise RpcShardingConfigError(
            f"{label} must be a literal IPv4 address (hostnames are refused), got {raw!r}"
        ) from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise RpcShardingConfigError(f"{label} must be an IPv4 address, got {raw!r}")
    if address.is_unspecified:
        raise RpcShardingConfigError(f"{label} must never be the unspecified address ({address})")
    if not any(address in network for network in ALLOWED_PEER_NETWORKS):
        raise RpcShardingConfigError(
            f"{label} {address} is not a private (10.0.0.0/8, 172.16.0.0/12, "
            "192.168.0.0/16) or link-local (169.254.0.0/16) IPv4 address"
        )
    return str(address)


def validate_rpc_port(value: object, *, label: str = "RPC port") -> int:
    """Return ``value`` when it is an integer TCP port in 1..65535."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RpcShardingConfigError(f"{label} must be an integer, got {value!r}")
    if not 1 <= value <= 65535:
        raise RpcShardingConfigError(f"{label} must be between 1 and 65535, got {value}")
    return value


def _parse_port_text(text: str, *, label: str) -> int:
    cleaned = text.strip()
    if not cleaned or len(cleaned) > 5 or not (cleaned.isascii() and cleaned.isdigit()):
        raise RpcShardingConfigError(f"{label} must be a decimal port number, got {text!r}")
    return validate_rpc_port(int(cleaned), label=label)


def parse_rpc_port(raw: str | None) -> int:
    """Parse ``SONDER_RPC_PORT``; unset or blank gives ``DEFAULT_RPC_PORT``."""
    if raw is None or not raw.strip():
        return DEFAULT_RPC_PORT
    return _parse_port_text(raw, label=RPC_PORT_ENV)


def parse_link_network(raw: str | None) -> ipaddress.IPv4Network | None:
    """Parse ``SONDER_RPC_LINK_CIDR``; unset or blank gives ``None`` (no link restriction).

    The value must be a strict IPv4 network (no host bits set) that lies wholly
    inside one of ``ALLOWED_PEER_NETWORKS``.
    """
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    try:
        network = ipaddress.ip_network(text, strict=True)
    except ValueError as exc:
        raise RpcShardingConfigError(
            f"{RPC_LINK_CIDR_ENV} must be an IPv4 network such as 10.77.0.0/30 with no "
            f"host bits set, got {text!r}"
        ) from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise RpcShardingConfigError(f"{RPC_LINK_CIDR_ENV} must be an IPv4 network, got {text!r}")
    if not any(network.subnet_of(allowed) for allowed in ALLOWED_PEER_NETWORKS):
        raise RpcShardingConfigError(
            f"{RPC_LINK_CIDR_ENV} {network} is not inside a private (10.0.0.0/8, "
            "172.16.0.0/12, 192.168.0.0/16) or link-local (169.254.0.0/16) range"
        )
    return network


def validate_host_on_link(
    host: str, network: ipaddress.IPv4Network | None, *, label: str = "RPC peer host"
) -> str:
    """Return ``host`` when ``network`` is ``None`` or contains it; raise otherwise."""
    if network is None:
        return host
    if ipaddress.IPv4Address(host) not in network:
        raise RpcShardingConfigError(
            f"{label} {host} is not on the configured private link {network} ({RPC_LINK_CIDR_ENV})"
        )
    return host


def parse_rpc_proto_major(raw: str | None) -> int | None:
    """Parse ``SONDER_RPC_PROTO_MAJOR``; unset or blank gives ``None`` (no pinned major)."""
    if raw is None or not raw.strip():
        return None
    cleaned = raw.strip()
    if len(cleaned) > 3 or not (cleaned.isascii() and cleaned.isdigit()):
        raise RpcShardingConfigError(
            f"{RPC_PROTO_MAJOR_ENV} must be a decimal llama.cpp RPC protocol major version, got {raw!r}"
        )
    value = int(cleaned)
    if not 1 <= value <= MAX_RPC_PROTO_MAJOR:
        raise RpcShardingConfigError(
            f"{RPC_PROTO_MAJOR_ENV} must be between 1 and {MAX_RPC_PROTO_MAJOR}, got {value}"
        )
    return value


def parse_peer_entry(entry: str, *, port: int) -> tuple[str, int]:
    """Parse one ``ip`` or ``ip:port`` allowlist entry that must use ``port``."""
    allowed_port = validate_rpc_port(port, label=RPC_PORT_ENV)
    text = entry.strip()
    if not text:
        raise RpcShardingConfigError("RPC peer entry must not be empty")
    if text.startswith("[") or text.count(":") > 1:
        raise RpcShardingConfigError(
            f"RPC peer {text!r} must be an IPv4 literal with an optional :port"
        )
    if ":" in text:
        host_text, port_text = text.split(":", 1)
        entry_port = _parse_port_text(port_text, label=f"RPC peer port in {text!r}")
        if entry_port != allowed_port:
            raise RpcShardingConfigError(
                f"RPC peer {text!r} uses port {entry_port}, but the configured RPC "
                f"port is {allowed_port} ({RPC_PORT_ENV})"
            )
    else:
        host_text = text
    return validate_peer_host(host_text), allowed_port


def parse_peer_allowlist(
    raw: str | None,
    *,
    port: int,
    link_network: ipaddress.IPv4Network | None = None,
) -> tuple[tuple[str, int], ...]:
    """Parse a comma- or semicolon-separated allowlist; duplicates collapse in order.

    With ``link_network`` every entry must also lie inside that network.
    """
    allowed_port = validate_rpc_port(port, label=RPC_PORT_ENV)
    tokens = [part.strip() for part in (raw or "").replace(";", ",").split(",")]
    entries = [token for token in tokens if token]
    if len(entries) > MAX_RPC_PEERS:
        raise RpcShardingConfigError(
            f"{RPC_PEERS_ENV} names {len(entries)} peers; at most {MAX_RPC_PEERS} are allowed"
        )
    peers: list[tuple[str, int]] = []
    for entry in entries:
        peer = parse_peer_entry(entry, port=allowed_port)
        validate_host_on_link(peer[0], link_network)
        if peer not in peers:
            peers.append(peer)
    return tuple(peers)


@dataclass(frozen=True)
class RpcPeerAllowlist:
    """The validated worker allowlist. Empty ``peers`` means sharding is inactive.

    ``link_network`` is the configured private link (``None`` when unset) and
    ``proto_major`` the pinned llama.cpp RPC protocol major (``None`` when unset).
    """

    port: int = DEFAULT_RPC_PORT
    peers: tuple[tuple[str, int], ...] = ()
    link_network: ipaddress.IPv4Network | None = None
    proto_major: int | None = None

    @property
    def active(self) -> bool:
        return bool(self.peers)


def rpc_peer_allowlist(env: Mapping[str, str]) -> RpcPeerAllowlist:
    """Read the ``SONDER_RPC_*`` allowlist settings from ``env``; raise on any fault.

    ``SONDER_RPC_PORT``, ``SONDER_RPC_LINK_CIDR`` and ``SONDER_RPC_PROTO_MAJOR``
    are validated even while ``SONDER_RPC_PEERS`` is unset.
    """
    port = parse_rpc_port(env.get(RPC_PORT_ENV))
    link_network = parse_link_network(env.get(RPC_LINK_CIDR_ENV))
    proto_major = parse_rpc_proto_major(env.get(RPC_PROTO_MAJOR_ENV))
    peers = parse_peer_allowlist(env.get(RPC_PEERS_ENV), port=port, link_network=link_network)
    return RpcPeerAllowlist(port=port, peers=peers, link_network=link_network, proto_major=proto_major)


def rpc_sharding_errors(env: Mapping[str, str]) -> list[str]:
    """Return the allowlist fault for ``env`` as a one-item list, or no errors."""
    try:
        rpc_peer_allowlist(env)
    except RpcShardingConfigError as exc:
        return [str(exc)]
    return []


__all__ = [
    "ALLOWED_PEER_NETWORKS",
    "DEFAULT_RPC_PORT",
    "MAX_RPC_PEERS",
    "MAX_RPC_PROTO_MAJOR",
    "RPC_LINK_CIDR_ENV",
    "RPC_PEERS_ENV",
    "RPC_PORT_ENV",
    "RPC_PROTO_MAJOR_ENV",
    "RpcPeerAllowlist",
    "RpcShardingConfigError",
    "parse_link_network",
    "parse_peer_allowlist",
    "parse_peer_entry",
    "parse_rpc_port",
    "parse_rpc_proto_major",
    "rpc_peer_allowlist",
    "rpc_sharding_errors",
    "validate_host_on_link",
    "validate_peer_host",
    "validate_rpc_port",
]
