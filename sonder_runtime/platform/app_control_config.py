"""Host-only app-control prerequisites. This module grants no account authority."""

from dataclasses import dataclass, field
import ipaddress
from pathlib import Path
import re


@dataclass(frozen=True)
class AppControlConfig:
    enabled: bool = False
    runtime_id: str = ""
    catalog_file: str = field(default="", repr=False)
    allow_numeric_loopback_native: bool = False
    app_origins: tuple[str, ...] = ()
    proxy_cidrs: tuple[str, ...] = ()
    proxy_only_backend: bool = False
    session_ttl_seconds: int = 900
    binding_ttl_seconds: int = 3600
    account_session_cap: int = 4
    global_session_cap: int = 64
    account_binding_cap: int = 16
    global_binding_cap: int = 256
    command_cap: int = 4096
    page_cap: int = 100


def canonical_https_origin(value):
    if type(value) is not str or len(value) > 512:
        raise ValueError("invalid app origin")
    match = re.fullmatch(
        r"https://(\[[0-9a-f:]+\]|[a-z0-9.-]+)(?::([0-9]{1,5}))?", value
    )
    if match is None:
        raise ValueError("invalid app origin")
    host = match.group(1).strip("[]")
    port_text = match.group(2)
    port = int(port_text) if port_text is not None else None
    if port is not None and (not 1 <= port <= 65535 or str(port) != port_text):
        raise ValueError("invalid app port")
    try:
        address = ipaddress.ip_address(host)
        if str(address) != host or "%" in host:
            raise ValueError("noncanonical app host")
    except ValueError:
        if (
            ":" in host
            or len(host) > 253
            or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            )
        ):
            raise ValueError("invalid app host") from None
    shown = f"[{host}]" if ":" in host else host
    canonical = f"https://{shown}" + (f":{port}" if port not in (None, 443) else "")
    if canonical != value:
        raise ValueError("app origin must be canonical")
    return value


def app_control_errors(config):
    c = config.app_control
    errors = []
    if type(c.runtime_id) is not str or type(c.catalog_file) is not str:
        return ["[app_control] invalid identifier or catalog path type"]
    if any(
        type(getattr(c, name)) is not tuple
        or any(type(value) is not str for value in getattr(c, name))
        for name in ("app_origins", "proxy_cidrs")
    ):
        return ["[app_control] immutable string tuples required"]
    for key in ("enabled", "allow_numeric_loopback_native", "proxy_only_backend"):
        if type(getattr(c, key)) is not bool:
            errors.append(f"[app_control].{key} must be boolean")
    if c.runtime_id and not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", c.runtime_id):
        errors.append("[app_control].runtime_id must be a stable bounded identifier")
    if c.catalog_file and (
        not Path(c.catalog_file).is_absolute()
        or len(c.catalog_file) > 4096
        or any(ord(c) < 32 for c in c.catalog_file)
    ):
        errors.append("[app_control].catalog_file must be an absolute private path")
    for key, maximum in (
        ("session_ttl_seconds", 3600),
        ("binding_ttl_seconds", 86400),
        ("account_session_cap", 64),
        ("global_session_cap", 1024),
        ("account_binding_cap", 256),
        ("global_binding_cap", 4096),
        ("command_cap", 65536),
        ("page_cap", 100),
    ):
        if type(getattr(c, key)) is not int or not 1 <= getattr(c, key) <= maximum:
            errors.append(f"[app_control].{key} exceeds bounds")
    for key in ("app_origins", "proxy_cidrs"):
        values = getattr(c, key)
        if (
            type(values) is not tuple
            or len(values) > 32
            or len(set(values)) != len(values)
        ):
            errors.append(f"[app_control].{key} must be a bounded unique tuple")
    try:
        for origin in c.app_origins:
            canonical_https_origin(origin)
        for cidr in c.proxy_cidrs:
            if str(ipaddress.ip_network(cidr, strict=True)) != cidr:
                raise ValueError()
    except (ValueError, TypeError):
        errors.append("[app_control] invalid origin or proxy CIDR")
    if not set(c.app_origins).issubset(config.server.cors_origins):
        errors.append("[app_control] origins must be a subset of CORS")
    if c.enabled:
        if not config.features.host_control or not c.runtime_id or not c.catalog_file:
            errors.append(
                "[app_control] requires host_control, runtime_id and private catalog"
            )
        if c.app_origins and (
            not config.server.tls_terminated_by_proxy
            or not c.proxy_cidrs
            or not c.proxy_only_backend
        ):
            errors.append(
                "[app_control] browser mode requires TLS proxy and proxy-only backend deployment"
            )
    return errors


def app_control_transport(config, *, raw_peer, origin):
    """Pure route predicate. Host must enforce proxy-only network deployment.

    raw_peer is the socket address, never any forwarded-header projection.
    This result is a transport prerequisite, never account authentication.
    """
    if not config.app_control.enabled or app_control_errors(config):
        return False
    c = config.app_control
    try:
        if type(raw_peer) is not str or "%" in raw_peer:
            return False
        peer = ipaddress.ip_address(raw_peer)
        if str(peer) != raw_peer:
            return False
        if origin is None and c.allow_numeric_loopback_native:
            if "%" in config.server.host:
                return False
            listener = ipaddress.ip_address(config.server.host)
            return (
                str(listener) == config.server.host
                and listener.is_loopback
                and peer.is_loopback
            )
        return bool(
            origin in c.app_origins
            and c.proxy_only_backend
            and config.server.tls_terminated_by_proxy
            and any(peer in ipaddress.ip_network(cidr) for cidr in c.proxy_cidrs)
        )
    except (ValueError, TypeError):
        return False
