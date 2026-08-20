"""Provider-neutral web and credential ports (WP3 SEAM-011).

The application owns the egress and credential constraints.  A concrete
provider owns sockets, credential material, and cleanup; this module contains
no HTTP, DNS, MCP, environment, or filesystem adapter logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping, Protocol
import re

from ..context import OperationContext


class WebPolicyError(ValueError):
    """Raised when a web request or policy is malformed or too broad."""


_URL_RE = re.compile(r"^(?P<scheme>https?)://(?P<authority>[^/?#]+)(?P<path>/[^?#]*)?(?:\?(?P<query>[^#]*))?(?:#(?P<fragment>.*))?$")


def _url_parts(url: str) -> tuple[str, str, str, str, str]:
    match = _URL_RE.fullmatch(url)
    if not match:
        return "", "", "", "", ""
    authority = match.group("authority")
    host = authority.rsplit("@", 1)[-1].split(":", 1)[0]
    return match.group("scheme"), host, authority, match.group("path") or "", match.group("fragment") or ""


class CredentialScope(StrEnum):
    """The maximum lifetime/audience of credential material."""

    REQUEST = "request"
    PROVIDER = "provider"


class ProviderHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """Fail-closed network constraints; providers may only narrow them."""

    allowed_hosts: tuple[str, ...]
    schemes: tuple[str, ...] = ("https",)
    max_response_bytes: int = 1_048_576
    max_redirects: int = 0
    require_cloud_consent: bool = True

    def __post_init__(self) -> None:
        if not self.allowed_hosts or any(
            not isinstance(host, str) or not host.strip() or "/" in host
            for host in self.allowed_hosts
        ):
            raise WebPolicyError("allowed_hosts must contain hostnames")
        if not self.schemes or any(s not in ("http", "https") for s in self.schemes):
            raise WebPolicyError("schemes must contain only http or https")
        if type(self.max_response_bytes) is not int or self.max_response_bytes <= 0:
            raise WebPolicyError("max_response_bytes must be positive")
        if type(self.max_redirects) is not int or not 0 <= self.max_redirects <= 5:
            raise WebPolicyError("max_redirects must be between 0 and 5")

    def allows(self, url: str, context: OperationContext) -> bool:
        scheme, host, authority, _, _ = _url_parts(url)
        return (
            scheme in self.schemes
            and host in self.allowed_hosts
            and (not self.require_cloud_consent or context.cloud_allowed)
        )


@dataclass(frozen=True, slots=True)
class WebRequest:
    url: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    credential_name: str | None = None

    def __post_init__(self) -> None:
        scheme, _, authority, _, fragment = _url_parts(self.url)
        if scheme not in ("http", "https") or not authority:
            raise WebPolicyError("url must be an absolute HTTP(S) URL")
        if "@" in authority or fragment:
            raise WebPolicyError("url userinfo and fragments are not allowed")
        if self.method not in {"GET", "HEAD", "POST"}:
            raise WebPolicyError("unsupported web method")
        if not isinstance(self.body, bytes):
            raise TypeError("body must be bytes")
        if any(k.lower() in {"authorization", "cookie", "proxy-authorization"} for k in self.headers):
            raise WebPolicyError("credential-bearing headers belong to CredentialProvider")
        if self.credential_name is not None and not self.credential_name.strip():
            raise WebPolicyError("credential_name must be non-empty")


@dataclass(frozen=True, slots=True)
class WebResponse:
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    redaction_applied: bool = True

    def __post_init__(self) -> None:
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise ValueError("status_code must be an HTTP status")
        if not isinstance(self.body, bytes):
            raise TypeError("body must be bytes")
        if not isinstance(self.redaction_applied, bool):
            raise TypeError("redaction_applied must be bool")


@dataclass(frozen=True, slots=True)
class CredentialRequest:
    name: str
    audience: str
    scope: CredentialScope = CredentialScope.REQUEST

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.audience.strip():
            raise WebPolicyError("credential name and audience are required")


@dataclass(frozen=True, slots=True, repr=False)
class CredentialLease:
    """Opaque credential capability.  Its representation never contains value."""

    name: str
    audience: str
    scope: CredentialScope
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.value:
            raise WebPolicyError("credential value must be non-empty")

    def __repr__(self) -> str:
        return f"CredentialLease(name={self.name!r}, audience={self.audience!r}, scope={self.scope.value!r}, value='<redacted>')"


@dataclass(frozen=True, slots=True)
class ProviderHealthSnapshot:
    status: ProviderHealth
    checked_at: datetime
    consecutive_failures: int = 0
    detail: str = ""

    def __post_init__(self) -> None:
        if self.checked_at.tzinfo is None:
            raise ValueError("checked_at must be timezone-aware")
        if type(self.consecutive_failures) is not int or self.consecutive_failures < 0:
            raise ValueError("consecutive_failures must be non-negative")
        if len(self.detail) > 256 or "\n" in self.detail:
            raise ValueError("health detail must be a short single line")


class CredentialProvider(Protocol):
    """Resolve host-owned credentials without exposing storage or secret scope."""

    # [any thread, async safe] Lease must not outlive the operation/request.
    def acquire(self, request: CredentialRequest, context: OperationContext) -> CredentialLease: ...

    # [any thread, thread-safe] Idempotent invalidation; value is not logged.
    def release(self, lease: CredentialLease) -> None: ...


class WebProvider(Protocol):
    """Perform bounded web requests under an explicit egress policy."""

    # [any thread, async safe] Provider owns transport and response cleanup.
    def request(self, request: WebRequest, policy: EgressPolicy, context: OperationContext) -> WebResponse: ...

    # [any thread, thread-safe] Safe metadata only; no endpoints or credentials.
    def health(self) -> ProviderHealthSnapshot: ...


def redact(text: str, secrets: tuple[str, ...]) -> str:
    """Replace known secret values before diagnostics or telemetry leave a provider."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "<redacted>")
    return result


__all__ = [
    "CredentialLease", "CredentialProvider", "CredentialRequest", "CredentialScope",
    "EgressPolicy", "ProviderHealth", "ProviderHealthSnapshot", "WebPolicyError",
    "WebProvider", "WebRequest", "WebResponse", "redact",
]
