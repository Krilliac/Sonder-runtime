"""Disabled trusted-peer configuration for future fact-only replication.

This module only validates static configuration.  It creates no listener,
receiver, journal, client, retry loop, or network connection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import re


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_HTTPS_ORIGIN = re.compile(
    r"https://(?P<host>\[[0-9a-f:]+\]|[a-z0-9.-]+):(?P<port>[0-9]{1,5})\Z"
)
_MAX_PEERS = 16
_MIN_SECRET_LENGTH = 32
_MAX_SECRET_LENGTH = 512


@dataclass(frozen=True)
class MemoryReplicationPeerConfig:
    """One fixed remote identity and its exact fact-scope HTTPS origin."""

    node_id: str = ""
    project_scope: str = ""
    origin: str = field(default="", repr=False)


@dataclass(frozen=True)
class MemoryReplicationConfig:
    """Static, disabled-by-default admission data for future replication."""

    enabled: bool = False
    local_node_id: str = ""
    project_scope: str = ""
    receiver_enabled: bool = False
    accepted_source_ids: tuple[str, ...] = ()
    peers: tuple[MemoryReplicationPeerConfig, ...] = ()
    request_timeout_seconds: int = 5
    max_request_bytes: int = 8 * 1024 * 1024
    max_response_bytes: int = 64 * 1024
    max_batch_records: int = 256


def _is_loopback_host(value: object) -> bool:
    if type(value) is not str:
        return False
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _is_identity(value: object) -> bool:
    return type(value) is str and _IDENTITY.fullmatch(value) is not None


def _is_project_scope(value: object) -> bool:
    return type(value) is str and _PROJECT.fullmatch(value) is not None


def _is_strict_https_origin(value: object) -> bool:
    """Validate a canonical credential-free HTTPS origin without reflection."""
    if type(value) is not str or not 1 <= len(value) <= 2048:
        return False
    match = _HTTPS_ORIGIN.fullmatch(value)
    if match is None:
        return False
    port = int(match.group("port"))
    if not 1 <= port <= 65535:
        return False
    host_token = match.group("host")
    host = host_token[1:-1] if host_token.startswith("[") else host_token
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (
            len(host) > 253
            or not all(_HOST_LABEL.fullmatch(label) for label in host.split("."))
        ):
            return False
        rendered_host = host
    else:
        if str(address) != host:
            return False
        rendered_host = f"[{host}]" if ":" in host else host
    return value == f"https://{rendered_host}:{port}"


def _bounded_integer(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _dedicated_secret_is_valid(value: object) -> bool:
    return (
        type(value) is str
        and _MIN_SECRET_LENGTH <= len(value) <= _MAX_SECRET_LENGTH
        and all(0x21 <= ord(char) <= 0x7E for char in value)
    )


def memory_replication_errors(config) -> list[str]:
    """Return static-boundary errors without reflecting secrets or URLs."""
    section = getattr(config, "memory_replication", None)
    if not isinstance(section, MemoryReplicationConfig):
        return ["[memory_replication] must use the typed configuration section"]

    errors: list[str] = []
    enabled_value = section.enabled
    receiver_enabled_value = section.receiver_enabled
    if type(enabled_value) is not bool:
        errors.append("[memory_replication].enabled must be a boolean")
    if type(receiver_enabled_value) is not bool:
        errors.append("[memory_replication].receiver_enabled must be a boolean")
    enabled = enabled_value is True
    receiver_enabled = receiver_enabled_value is True

    local_node_id = section.local_node_id
    project_scope = section.project_scope
    local_node_id_is_exact_string = type(local_node_id) is str
    project_scope_is_exact_string = type(project_scope) is str

    if not local_node_id_is_exact_string:
        errors.append(
            "[memory_replication].local_node_id must be a bounded stable identity"
        )
    elif local_node_id:
        if not _is_identity(local_node_id):
            errors.append(
                "[memory_replication].local_node_id must be a bounded stable identity"
            )
    elif enabled:
        errors.append(
            "[memory_replication].local_node_id must be a bounded stable identity"
        )

    if not project_scope_is_exact_string:
        errors.append(
            "[memory_replication].project_scope must be an exact bounded scope"
        )
    elif project_scope:
        if not _is_project_scope(project_scope):
            errors.append(
                "[memory_replication].project_scope must be an exact bounded scope"
            )
    elif enabled:
        errors.append(
            "[memory_replication].project_scope must be an exact bounded scope"
        )

    for name, minimum, maximum in (
        ("request_timeout_seconds", 1, 30),
        ("max_request_bytes", 1024, 16 * 1024 * 1024),
        ("max_response_bytes", 1024, 1024 * 1024),
        ("max_batch_records", 1, 256),
    ):
        if not _bounded_integer(getattr(section, name), minimum, maximum):
            errors.append(f"[memory_replication].{name} must be within {minimum}..{maximum}")

    accepted = section.accepted_source_ids
    accepted_is_tuple = type(accepted) is tuple
    accepted_are_strings = accepted_is_tuple and all(
        type(source_id) is str for source_id in accepted
    )
    if not accepted_is_tuple:
        errors.append("[memory_replication].accepted_source_ids must be an immutable tuple")
        accepted = ()
    elif len(accepted) > _MAX_PEERS:
        errors.append(
            f"[memory_replication].accepted_source_ids supports at most {_MAX_PEERS} identities"
        )
    if not accepted_are_strings:
        errors.append(
            "[memory_replication].accepted_source_ids must contain bounded stable identities"
        )
    elif len(set(accepted)) != len(accepted):
        errors.append("[memory_replication].accepted_source_ids must not contain duplicates")
    elif any(not _is_identity(source_id) for source_id in accepted):
        errors.append(
            "[memory_replication].accepted_source_ids must contain bounded stable identities"
        )
    if (
        accepted_are_strings
        and local_node_id_is_exact_string
        and local_node_id in accepted
    ):
        errors.append("[memory_replication] local identity cannot be an accepted source")
    if accepted and not receiver_enabled:
        errors.append(
            "[memory_replication].accepted_source_ids requires receiver_enabled=true"
        )

    peers = section.peers
    if type(peers) is not tuple:
        errors.append("[memory_replication].peers must be an immutable tuple")
        peers = ()
    elif len(peers) > _MAX_PEERS:
        errors.append(f"[memory_replication].peers supports at most {_MAX_PEERS} entries")

    peer_ids: list[str] = []
    for index, peer in enumerate(peers):
        where = f"[memory_replication].peers[{index}]"
        if not isinstance(peer, MemoryReplicationPeerConfig):
            errors.append(f"{where} must be a typed fixed peer")
            continue
        peer_node_id = peer.node_id
        peer_project_scope = peer.project_scope
        peer_origin = peer.origin
        if not _is_identity(peer_node_id):
            errors.append(f"{where}.node_id must be a bounded stable identity")
        else:
            peer_ids.append(peer_node_id)
        if not _is_project_scope(peer_project_scope):
            errors.append(f"{where}.project_scope must be an exact bounded scope")
        elif project_scope_is_exact_string and project_scope != peer_project_scope:
            errors.append(
                f"{where}.project_scope must exactly match [memory_replication].project_scope"
            )
        if not _is_strict_https_origin(peer_origin):
            errors.append(f"{where}.origin must be a canonical HTTPS origin with an explicit port")

    if len(set(peer_ids)) != len(peer_ids):
        errors.append("[memory_replication].peers must not contain duplicate identities")
    if (
        local_node_id_is_exact_string
        and local_node_id
        and local_node_id in peer_ids
    ):
        errors.append("[memory_replication] local identity cannot be an outbound peer")

    if receiver_enabled and not enabled:
        errors.append("[memory_replication].receiver_enabled requires enabled=true")
    if enabled:
        if not peers:
            errors.append("[memory_replication] requires at least one fixed outbound peer")
        secrets = getattr(config, "secrets", None)
        key = getattr(secrets, "memory_replication_key", None)
        if not _dedicated_secret_is_valid(key):
            errors.append(
                "memory replication requires a dedicated 32..512 character secret"
            )
        else:
            state_integrity_key = getattr(
                secrets, "memory_replication_state_integrity_key", None,
            )
            if not _dedicated_secret_is_valid(state_integrity_key):
                errors.append(
                    "memory replication requires a dedicated local state-integrity "
                    "32..512 character secret"
                )
            else:
                api_key = getattr(secrets, "api_key", None)
                artifact_key = getattr(secrets, "artifact_transfer_key", None)
                mobility_key = getattr(secrets, "artifact_mobility_peer_key", None)
                auth_secret = getattr(secrets, "auth_secret", None)
                if not all(
                    type(value) is str
                    for value in (
                        key,
                        state_integrity_key,
                        api_key,
                        artifact_key,
                        mobility_key,
                        auth_secret,
                    )
                ):
                    errors.append(
                        "memory replication secret separation requires exact builtin strings"
                    )
                elif (
                    key == api_key
                    or key == artifact_key
                    or key == mobility_key
                    or key == auth_secret
                ):
                    errors.append(
                        "memory replication dedicated key must be distinct from API, "
                        "artifact-transfer, artifact-mobility, and auth secrets"
                    )
                elif (
                    state_integrity_key == key
                    or state_integrity_key == api_key
                    or state_integrity_key == artifact_key
                    or state_integrity_key == mobility_key
                    or state_integrity_key == auth_secret
                ):
                    errors.append(
                        "memory replication local state-integrity secret must be distinct "
                        "from replication peer, API, artifact-transfer, artifact-mobility, "
                        "and auth secrets"
                    )
        if receiver_enabled:
            if not accepted:
                errors.append(
                    "[memory_replication].receiver_enabled requires accepted_source_ids"
                )
            if not accepted_are_strings or not set(accepted).issubset(peer_ids):
                errors.append(
                    "[memory_replication].accepted_source_ids must name fixed configured peers"
                )
            server = getattr(config, "server", None)
            server_host = getattr(server, "host", None)
            tls_terminated_by_proxy = getattr(
                server, "tls_terminated_by_proxy", None,
            )
            if not _is_loopback_host(server_host) and (
                tls_terminated_by_proxy is not True
            ):
                errors.append(
                    "[memory_replication].receiver_enabled requires a loopback listener or declared TLS proxy"
                )
    return errors


__all__ = [
    "MemoryReplicationConfig",
    "MemoryReplicationPeerConfig",
    "memory_replication_errors",
]
