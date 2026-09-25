"""Request-origin policy for the HTTP listener: Host allowlist and client IP.

Two decisions live here because both are about *which peer and which name*
a request really came through, and both must be made before routing:

* ``host_decision`` defeats DNS rebinding.  A loopback-bound, local-open
  listener needs no credentials, and a same-origin browser ``GET`` sends no
  ``Origin`` header, so the origin check alone cannot tell a rebinding page
  (``http://rebind.attacker.example:11435``) from the operator's own tools.
  The ``Host`` header can: a browser always sends the name it resolved, and a
  rebinding attack only works through a hostname the attacker controls.
  Addresses and the machine's own names are therefore always accepted, and
  other names are refused only where they could matter: on a listener that
  answers without credentials.
* ``forwarded_client_ip`` decides whether ``X-Forwarded-For`` may name the
  client.  It may only when the operator declared a TLS-terminating proxy
  *and* the raw socket peer lies inside the trusted proxy CIDRs; otherwise
  any local process could rotate the header to dodge the authentication
  failure limiter or to lock out someone else's address.

Both functions are pure so the policy can be tested without a socket.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Iterable

__all__ = [
    "HOST_CREDENTIALED",
    "HOST_NOT_ALLOWED_REMEDY",
    "HOST_TRUSTED",
    "forwarded_client_ip",
    "host_allowed",
    "host_decision",
    "machine_host_names",
    "normalize_allowed_host",
    "parse_host_header",
]

_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_MAX_HOST_HEADER = 1024


def _normalize_name(name: str) -> str:
    name = name.strip().lower()
    if name.endswith(".") and not name.endswith(".."):
        name = name[:-1]
    return name


def _valid_dns_name(name: str) -> bool:
    if not name or len(name) > 253:
        return False
    return all(_HOST_LABEL.match(label) for label in name.split("."))


def parse_host_header(value) -> tuple[str, int | None] | None:
    """Split a ``Host`` value into ``(name, port)``; ``None`` when malformed.

    IPv6 literals keep no brackets in the returned name.  A name that is not a
    syntactically valid DNS name or IP literal is malformed: anything a
    browser could send for a rebinding page is a DNS name, so rejecting junk
    here never costs a legitimate client.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > _MAX_HOST_HEADER:
        return None
    port_text = ""
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        name = value[1:end]
        rest = value[end + 1:]
        if rest:
            if not rest.startswith(":"):
                return None
            port_text = rest[1:]
        try:
            if ipaddress.ip_address(name).version != 6:
                return None
        except ValueError:
            return None
        name = name.lower()
    else:
        if value.count(":") > 1:
            # An unbracketed IPv6 literal is not a valid Host (RFC 9110 7.2).
            return None
        name, sep, port_text = value.partition(":")
        if sep and not port_text:
            return None
        name = _normalize_name(name)
        try:
            ipaddress.ip_address(name)
        except ValueError:
            if not _valid_dns_name(name):
                return None
    port = None
    if port_text:
        if not port_text.isdigit() or len(port_text) > 5:
            return None
        port = int(port_text)
        if not 1 <= port <= 65535:
            return None
    return name, port


def normalize_allowed_host(entry) -> tuple[str, int | None]:
    """Validate one configured public host (``name`` or ``name:port``)."""
    parsed = parse_host_header(entry if isinstance(entry, str) else None)
    if parsed is None:
        raise ValueError("allowed host entry must be a DNS name or IP literal with an optional port")
    return parsed


def _ip(name: str):
    try:
        return ipaddress.ip_address(name)
    except ValueError:
        return None


def _is_loopback_name(name: str) -> bool:
    if name == "localhost" or name.endswith(".localhost"):
        return True
    address = _ip(name)
    return bool(address is not None and address.is_loopback)


def machine_host_names(hostname="", fqdn="") -> frozenset:
    """The names this machine answers to: host name, FQDN and ``<host>.local``.

    Pure: the caller (the listener, once at startup) supplies what
    ``socket.gethostname``/``socket.getfqdn`` returned.  Values that are not
    valid DNS names, and names that are really IP literals, are dropped; an
    IP literal is accepted on its own terms anyway.
    """
    names = set()
    for raw in (hostname, fqdn):
        if not isinstance(raw, str):
            continue
        name = _normalize_name(raw)
        if not name or _ip(name) is not None or not _valid_dns_name(name):
            continue
        names.add(name)
        short = name.split(".", 1)[0]
        if short and _valid_dns_name(short):
            names.add(short)
            names.add(short + ".local")
    return frozenset(names)


# Host decisions. ``TRUSTED`` names cannot carry a DNS-rebinding attack (a
# literal address, a loopback or own-machine name, an operator-listed name,
# or no Host at all); ``CREDENTIALED`` is any other well-formed name, accepted
# only because the listener demands credentials a rebinding page never has.
HOST_TRUSTED = "trusted"
HOST_CREDENTIALED = "credentialed"

# The remedy a refused client can act on; the 421 body carries it verbatim.
HOST_NOT_ALLOWED_REMEDY = (
    "connect with the server's IP address (or 127.0.0.1), or add this name "
    "to [server].allowed_hosts / SONDER_ALLOWED_HOSTS on the server"
)


def host_decision(value, *, allowed_hosts: Iterable = (), local_names: Iterable = (),
                  credentials_required: bool = False) -> str | None:
    """Classify a request's ``Host`` header; ``None`` means refuse (421).

    DNS rebinding only works through a hostname the attacker controls, and
    only against a listener that answers without credentials. So:

    * a missing header is accepted: HTTP/1.0 tooling may omit it and no
      browser can, so it cannot carry a rebinding attack;
    * a malformed header, or more than one, is refused;
    * any IP literal is accepted on any port (emulator ``10.0.2.2``, LAN and
      Tailscale addresses, port forwards), except the unspecified address,
      which browsers historically routed to localhost;
    * ``localhost``, ``*.localhost``, and this machine's own names
      (``local_names``: host name, FQDN, ``<host>.local``) on any port;
    * ``[server].allowed_hosts`` entries (``name`` on any port, ``name:port``
      on that port only);
    * any other well-formed name is ``CREDENTIALED`` when the listener
      requires credentials (API key or accounts) and refused in the
      unauthenticated local-open mode.
    """
    if value is None:
        return HOST_TRUSTED
    parsed = parse_host_header(value)
    if parsed is None:
        return None
    name, port = parsed
    address = _ip(name)
    if address is not None:
        return None if address.is_unspecified else HOST_TRUSTED
    if _is_loopback_name(name):
        return HOST_TRUSTED
    for entry in allowed_hosts or ():
        try:
            allowed_name, allowed_port = (
                entry if isinstance(entry, tuple) else normalize_allowed_host(entry)
            )
        except ValueError:
            continue
        if name == allowed_name and (allowed_port is None or allowed_port == port):
            return HOST_TRUSTED
    if name in {_normalize_name(str(item)) for item in (local_names or ())}:
        return HOST_TRUSTED
    return HOST_CREDENTIALED if credentials_required else None


def host_allowed(value, *, allowed_hosts: Iterable = (), local_names: Iterable = (),
                 credentials_required: bool = False) -> bool:
    """Whether a request's ``Host`` header may reach this listener at all."""
    return host_decision(
        value, allowed_hosts=allowed_hosts, local_names=local_names,
        credentials_required=credentials_required,
    ) is not None


def forwarded_client_ip(peer: str, forwarded_for: str, *, proxy_declared: bool,
                        trusted_networks: Iterable) -> str:
    """Resolve the client address, trusting ``X-Forwarded-For`` only via a proxy.

    The header is consulted only when ``proxy_declared`` (the operator set
    ``tls_terminated_by_proxy``) and the raw peer is inside a trusted proxy
    network.  It is then read right to left, skipping trusted proxy hops, so a
    client-supplied prefix cannot choose the address; the first entry that is
    not a trusted proxy is the client.  Malformed entries fail back to the
    peer rather than inventing an address.
    """
    peer = str(peer or "")
    if not peer or not proxy_declared:
        return peer
    networks = tuple(trusted_networks or ())
    try:
        peer_address = _unmapped(ipaddress.ip_address(peer))
    except ValueError:
        return peer
    if not any(peer_address in net for net in networks):
        return peer
    hops = [part.strip() for part in str(forwarded_for or "").split(",") if part.strip()]
    for hop in reversed(hops):
        try:
            address = _unmapped(ipaddress.ip_address(hop))
        except ValueError:
            return peer
        if not any(address in net for net in networks):
            return str(address)
    return peer


def _unmapped(address):
    """IPv4 view of an IPv4-mapped IPv6 address (``::ffff:a.b.c.d``).

    A dual-stack listener reports IPv4 peers in mapped form, which never
    matches an IPv4 trusted-proxy network, so a declared IPv4 proxy would be
    silently ignored.
    """
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address
