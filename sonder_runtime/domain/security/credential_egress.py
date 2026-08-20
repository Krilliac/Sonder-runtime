"""Pure credential scoping and outbound-egress policy decisions.

This module deliberately performs no DNS lookup or network I/O.  Callers must
validate the concrete destination for every request and every redirect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from ipaddress import ip_address
import re
import secrets


class EgressDenied(ValueError):
    """Raised when a destination or credential use is outside its scope."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CredentialHandle:
    """Opaque, non-secret reference to a narrowly scoped credential."""

    value: str
    issuer: str
    hosts: tuple[str, ...]
    protocols: tuple[str, ...] = ("https",)
    expires_at: datetime | None = None

    @classmethod
    def mint(cls, issuer: str, hosts: tuple[str, ...], protocols: tuple[str, ...] = ("https",), expires_at: datetime | None = None) -> "CredentialHandle":
        if not issuer or not hosts:
            raise ValueError("issuer and at least one host scope are required")
        normalized_protocols = tuple(sorted({p.lower() for p in protocols}))
        if not normalized_protocols or any(p not in {"https", "http"} for p in normalized_protocols):
            raise ValueError("unsupported credential protocol")
        return cls(secrets.token_urlsafe(24), issuer, tuple(sorted({_normalize_host(h) for h in hosts})), normalized_protocols, expires_at)

    def allows(self, url: str, *, now: datetime | None = None) -> bool:
        target = EgressTarget.parse(url)
        if target.protocol not in self.protocols or not _host_matches(target.host, self.hosts):
            return False
        return self.expires_at is None or (now or _utc_now()) < self.expires_at


@dataclass(frozen=True)
class EgressTarget:
    protocol: str
    host: str
    port: int | None
    url: str

    @classmethod
    def parse(cls, url: str) -> "EgressTarget":
        match = re.fullmatch(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<authority>[^/?#]+)(?P<rest>[/?#].*)?", url)
        if not match:
            raise EgressDenied("only absolute HTTP(S) URLs without userinfo are allowed")
        protocol = match.group("scheme").lower()
        authority = match.group("authority")
        if protocol not in {"https", "http"} or "@" in authority:
            raise EgressDenied("only absolute HTTP(S) URLs without userinfo are allowed")
        if authority.startswith("["):
            end = authority.find("]")
            if end < 0:
                raise EgressDenied("invalid destination host")
            host = authority[1:end].lower()
            suffix = authority[end + 1:]
            if suffix and not suffix.startswith(":"):
                raise EgressDenied("invalid destination host")
            port_text = suffix[1:] if suffix else ""
        else:
            host, separator, port_text = authority.partition(":")
            if separator and (":" in port_text or not port_text.isdigit()):
                raise EgressDenied("invalid destination host")
            host = host.rstrip(".").lower()
        if not host:
            raise EgressDenied("invalid destination host")
        try:
            port = int(port_text) if port_text else None
        except ValueError as exc:  # pragma: no cover - guarded by digit check
            raise EgressDenied("invalid destination port") from exc
        if port is not None and not 1 <= port <= 65535:
            raise EgressDenied("invalid destination port")
        return cls(protocol, host, port, url)


@dataclass(frozen=True)
class EgressPolicy:
    allowed_hosts: tuple[str, ...] = ()
    allowed_protocols: tuple[str, ...] = ("https",)
    deny_private_networks: bool = True
    deny_loopback: bool = True
    deny_link_local: bool = True

    def check(self, url: str) -> EgressTarget:
        target = EgressTarget.parse(url)
        if target.protocol not in {p.lower() for p in self.allowed_protocols}:
            raise EgressDenied("protocol is not allowed")
        if self.allowed_hosts and not _host_matches(target.host, tuple(_normalize_host(h) for h in self.allowed_hosts)):
            raise EgressDenied("host is not allowlisted")
        try:
            address = ip_address(target.host)
        except ValueError:
            address = None
        if address is not None and ((self.deny_loopback and address.is_loopback) or (self.deny_link_local and address.is_link_local) or (self.deny_private_networks and address.is_private)):
            raise EgressDenied("private, loopback, or link-local destination is denied")
        return target


@dataclass(frozen=True)
class RedirectChain:
    """Validates each hop independently; redirects never inherit approval."""

    hops: tuple[str, ...] = field(default_factory=tuple)

    def validate(self, policy: EgressPolicy, handle: CredentialHandle | None = None) -> tuple[EgressTarget, ...]:
        checked = tuple(policy.check(url) for url in self.hops)
        if handle and any(not handle.allows(target.url) for target in checked):
            raise EgressDenied("redirect destination is outside credential scope")
        return checked


def _normalize_host(host: str) -> str:
    value = host.strip().lower().rstrip(".")
    if not value or "/" in value or "://" in value:
        raise ValueError("invalid host scope")
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid host scope") from exc


def _host_matches(host: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(host, pattern) and (not pattern.startswith("*.") or host != pattern[2:]) for pattern in patterns)
