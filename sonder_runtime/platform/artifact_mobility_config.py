"""Disabled-by-default fixed-peer outbound artifact mobility configuration."""

from dataclasses import dataclass, field
import ipaddress
import re

from sonder_runtime.domain.artifact_mobility_label import is_public_mobility_label


_MAX_DESTINATION_ORIGIN_LENGTH = 512
_MAX_DESTINATION_AUTHORITY_LENGTH = 512
_MAX_PORT_TEXT_LENGTH = 5


@dataclass(frozen=True)
class ArtifactMobilityConfig:
    enabled: bool = False
    destination_label: str = ""
    destination_origin: str = field(default="", repr=False)
    destination_tls_certificate_sha256: str = field(default="", repr=False)
    expected_recipient_attestation_sha256: str = field(default="", repr=False)
    destination_credential_id: str = field(default="", repr=False)
    max_object_bytes: int = 256 * 1024 * 1024
    attempt_timeout_seconds: int = 30
    attempt_lease_seconds: int = 90
    receipt_ttl_seconds: int = 7 * 24 * 60 * 60
    max_live_operations: int = 64


def _identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(33 <= ord(character) <= 126 for character in value)
    )


def _pin(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _origin_host(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        labels = value.split(".")
        return bool(labels) and all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            is not None
            for label in labels
        )


def _strict_https_origin(value: object) -> bool:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_DESTINATION_ORIGIN_LENGTH
        or not value.startswith("https://")
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        return False
    authority = value[len("https://"):]
    if authority.endswith("/"):
        authority = authority[:-1]
    if (
        not authority
        or len(authority) > _MAX_DESTINATION_AUTHORITY_LENGTH
        or any(character in authority for character in "/?#@")
    ):
        return False
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 1 or authority[closing + 1:closing + 2] != ":":
            return False
        host = authority[1:closing]
        port_text = authority[closing + 2:]
        try:
            if not isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
                return False
        except ValueError:
            return False
    else:
        host, separator, port_text = authority.rpartition(":")
        if not separator or not host or ":" in host or not _origin_host(host):
            return False
    if (
        not port_text.isascii()
        or not port_text.isdecimal()
        or len(port_text) > _MAX_PORT_TEXT_LENGTH
    ):
        return False
    try:
        port = int(port_text)
    except (TypeError, ValueError, OverflowError):
        return False
    return 1 <= port <= 65_535


def _peer_key(value: object) -> bool:
    return (
        isinstance(value, str)
        and 32 <= len(value) <= 512
        and all(33 <= ord(character) <= 126 for character in value)
        and "://" not in value
        and not value.startswith("//")
        and re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) is None
    )


def artifact_mobility_errors(config) -> list[str]:
    section = config.artifact_mobility
    errors: list[str] = []
    if type(section.enabled) is not bool:
        errors.append("[artifact_mobility].enabled invalid")
    for name, valid in (
        ("destination_label", is_public_mobility_label),
        ("destination_credential_id", _identifier),
    ):
        value = getattr(section, name)
        if (section.enabled and not valid(value)) or (
            value and not valid(value)
        ):
            errors.append(f"[artifact_mobility].{name} invalid")
    if (section.enabled and not _strict_https_origin(section.destination_origin)) or (
        section.destination_origin and not _strict_https_origin(section.destination_origin)
    ):
        errors.append("[artifact_mobility].destination_origin invalid")
    for name in (
        "destination_tls_certificate_sha256",
        "expected_recipient_attestation_sha256",
    ):
        value = getattr(section, name)
        if (section.enabled and not _pin(value)) or (value and not _pin(value)):
            errors.append(f"[artifact_mobility].{name} invalid")
    bounds = {
        "max_object_bytes": (1, 64 * 1024**3),
        "attempt_timeout_seconds": (1, 30),
        "attempt_lease_seconds": (2, 3600),
        "receipt_ttl_seconds": (60, 31 * 24 * 60 * 60),
        "max_live_operations": (1, 256),
    }
    for name, (minimum, maximum) in bounds.items():
        value = getattr(section, name)
        if type(value) is not int or not minimum <= value <= maximum:
            errors.append(f"[artifact_mobility].{name} invalid")
    if (
        type(section.attempt_timeout_seconds) is int
        and type(section.attempt_lease_seconds) is int
        and section.attempt_lease_seconds <= section.attempt_timeout_seconds
    ):
        errors.append("[artifact_mobility].attempt_lease_seconds invalid")
    key = config.secrets.artifact_mobility_peer_key
    key_configured = key != ""
    if key_configured and not _peer_key(key):
        errors.append("[artifact_mobility].peer_key invalid")
    elif key_configured:
        separated = (
            config.secrets.api_key,
            config.secrets.auth_secret,
            config.secrets.artifact_transfer_key,
            config.secrets.memory_replication_key,
            config.secrets.memory_replication_state_integrity_key,
        )
        if not all(type(value) is str for value in (key, *separated)):
            errors.append(
                "[artifact_mobility].peer_key separation requires exact builtin strings"
            )
        elif key in separated:
            errors.append("[artifact_mobility].peer_key must be distinct")
    if not section.enabled:
        return errors
    if not config.artifact_mobility_source.enabled:
        errors.append("[artifact_mobility] requires enabled source configuration")
    elif (
        type(section.max_object_bytes) is int
        and type(config.artifact_mobility_source.max_object_bytes) is int
        and section.max_object_bytes > config.artifact_mobility_source.max_object_bytes
    ):
        errors.append("[artifact_mobility].max_object_bytes invalid")
    if not key_configured:
        errors.append("[artifact_mobility].peer_key invalid")
    return errors
