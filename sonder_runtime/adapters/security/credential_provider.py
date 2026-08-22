"""Credential-provider adapter backed by the typed application broker.

The adapter stores only opaque handles.  The broker remains the sole owner of
secret values and authorizes each concrete URL independently.
"""

from __future__ import annotations

from typing import Mapping

from ...application.context import OperationContext
from ...application.ports.web import CredentialLease, CredentialProvider, CredentialRequest
from ...application.security.credential_broker import CredentialBroker
from ...domain.security.credential_egress import CredentialHandle, EgressDenied


class BrokerCredentialProvider:
    """Resolve named credentials without exposing the broker's secret store."""

    def __init__(self, broker: CredentialBroker, handles: Mapping[str, CredentialHandle] | None = None) -> None:
        self._broker = broker
        self._handles = dict(handles or {})

    def register(self, name: str, handle: CredentialHandle) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("credential name must be non-empty")
        self._handles[name] = handle

    def acquire(self, request: CredentialRequest, context: OperationContext) -> CredentialLease:
        del context
        handle = self._handles.get(request.name)
        if handle is None:
            raise EgressDenied("unknown credential name")
        use = self._broker.authorize(handle, request.audience)
        return CredentialLease(
            name=request.name,
            audience=request.audience,
            scope=request.scope,
            value=f"{use.header_name}\x00{use.header_value}",
        )

    def release(self, lease: CredentialLease) -> None:
        del lease


__all__ = ["BrokerCredentialProvider"]
