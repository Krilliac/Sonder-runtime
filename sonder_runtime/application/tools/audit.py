"""Typed durable audit boundary for tool gateway receipts."""
from __future__ import annotations

from typing import Protocol

from .gateway_contract import ToolGatewayRequest, ToolReceipt


class ToolAuditError(RuntimeError):
    """Raised when a tool audit record cannot be made safe and durable."""


class ToolAuditRepository(Protocol):
    def append(self, request: ToolGatewayRequest, receipt: ToolReceipt) -> None: ...

    def verify(self) -> None: ...


__all__ = ["ToolAuditError", "ToolAuditRepository"]
