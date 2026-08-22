"""Ports used by the provider-neutral protocol application boundary."""
from __future__ import annotations

from typing import Protocol


class ProtocolAuthorization(Protocol):
    """Authorize one protocol operation for one client principal.

    Implementations belong to the hosting interface.  The application facade
    never interprets bearer tokens or account records; it only consumes this
    already-authorized decision and fails closed when the port is absent.
    """

    def authorize(self, operation: str, client_id: str) -> bool: ...


__all__ = ["ProtocolAuthorization"]
