"""Typed, fail-closed configuration for the explicit control-state rehearsal."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ControlStateRehearsalConfig:
    enabled: bool = False
    cluster_id: str = ""
    node_id: str = ""
    witness_id: str = ""
    provider_id: str = ""
    origin: str = ""
    timeout_seconds: int = 5
    allow_insecure_loopback: bool = False


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _origin_error(origin: object, *, allow_insecure_loopback: bool) -> str | None:
    if not isinstance(origin, str) or not origin or len(origin) > 2048:
        return "[control_state_rehearsal].origin must be a bounded HTTP(S) origin"
    try:
        parsed = urlsplit(origin)
        parsed.port
    except ValueError:
        return "[control_state_rehearsal].origin has an invalid port"
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return (
            "[control_state_rehearsal].origin must be an HTTP(S) origin "
            "without credentials"
        )
    if parsed.scheme == "http" and not (
        allow_insecure_loopback and _is_loopback_host(parsed.hostname)
    ):
        return "remote control-state rehearsal origin must use HTTPS"
    return None


def control_state_rehearsal_errors(config) -> list[str]:
    """Collect configuration errors without constructing a provider or making I/O."""
    section = config.control_state_rehearsal
    errors: list[str] = []
    if type(section.enabled) is not bool:
        return ["[control_state_rehearsal].enabled must be a boolean"]
    if type(section.allow_insecure_loopback) is not bool:
        errors.append(
            "[control_state_rehearsal].allow_insecure_loopback must be a boolean"
        )
    if (
        type(section.timeout_seconds) is not int
        or not 1 <= section.timeout_seconds <= 30
    ):
        errors.append("[control_state_rehearsal].timeout_seconds must be within 1..30")
    if not section.enabled:
        return errors

    if config.deployment.profile != "pooled-pair":
        errors.append(
            "[control_state_rehearsal] requires "
            "[deployment].profile = \"pooled-pair\""
        )
    for name in ("cluster_id", "node_id", "witness_id", "provider_id"):
        value = getattr(section, name)
        if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
            errors.append(
                f"[control_state_rehearsal].{name} must be a bounded stable identity"
            )

    members = (config.compute.node_id, *(node.node_id for node in config.compute.nodes))
    if section.node_id != config.compute.node_id:
        errors.append("[control_state_rehearsal].node_id must match [compute].node_id")
    if section.witness_id in members:
        errors.append(
            "[control_state_rehearsal].witness_id must differ from both "
            "data replica identities"
        )
    if section.node_id == section.witness_id:
        errors.append("[control_state_rehearsal].node_id and witness_id must be distinct")

    origin_error = _origin_error(
        section.origin,
        allow_insecure_loopback=section.allow_insecure_loopback,
    )
    if origin_error:
        errors.append(origin_error)
    key = config.secrets.control_state_rehearsal_key
    if not isinstance(key, str) or not 1 <= len(key) <= 512 or any(
        ord(char) < 0x21 or ord(char) > 0x7E for char in key
    ):
        errors.append("SONDER_CONTROL_STATE_REHEARSAL_API_KEY is required")
    return errors
