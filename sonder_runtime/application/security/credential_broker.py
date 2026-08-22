"""Application broker for opaque, policy-bound credential use."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from sonder_runtime.domain.security.credential_egress import CredentialHandle, EgressDenied, EgressPolicy, EgressTarget


@dataclass(frozen=True)
class CredentialUse:
    handle: CredentialHandle
    header_name: str
    header_value: str
    target: EgressTarget


class CredentialBroker:
    """Keeps secret values behind handles and returns only authorized use data."""

    def __init__(self, policy: EgressPolicy):
        self._policy = policy
        self._secrets: dict[str, tuple[CredentialHandle, str, str]] = {}

    def issue(self, *, issuer: str, secret: str, hosts: tuple[str, ...], header_name: str = "Authorization", protocols: tuple[str, ...] = ("https",), expires_at: datetime | None = None) -> CredentialHandle:
        if (
            not secret
            or any(char in secret for char in "\r\n\x00")
            or not header_name
            or any(char in header_name for char in "\r\n\x00:")
        ):
            raise ValueError("credential secret and safe header name are required")
        handle = CredentialHandle.mint(issuer, hosts, protocols, expires_at)
        self._secrets[handle.value] = (handle, header_name, secret)
        return handle

    def authorize(self, handle: CredentialHandle, url: str, *, now: datetime | None = None) -> CredentialUse:
        stored = self._secrets.get(handle.value)
        if stored is None or stored[0] != handle:
            raise EgressDenied("unknown credential handle")
        target = self._policy.check(url)
        if not handle.allows(url, now=now):
            raise EgressDenied("credential is outside its scope or expired")
        effective_now = now or datetime.now(timezone.utc)
        if handle.expires_at is not None and effective_now >= handle.expires_at:
            raise EgressDenied("credential is expired")
        return CredentialUse(handle, stored[1], stored[2], target)

    def revoke(self, handle: CredentialHandle) -> None:
        self._secrets.pop(handle.value, None)
