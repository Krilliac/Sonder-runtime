"""Provider-neutral ToolService gateway contract.

This is an application seam, not a provider implementation.  It makes the
cross-cutting rules around a tool invocation explicit: typed input, scope and
permission checks, approval, deadline/cancellation checks, invocation,
output redaction, and receipt publication.  The collaborators are ports and
must not perform provider, network, filesystem, or process I/O here.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol

from ...domain.common.errors import Cancelled, DeadlineExceeded, Forbidden, InvalidInput


class ApprovalMode(Enum):
    NOT_REQUIRED = "not_required"
    REQUIRED = "required"


@dataclass(frozen=True)
class ToolScope:
    """Request scope supplied by the caller; it is never widened by a tool."""

    principal_id: str
    workspace_roots: tuple[str, ...] = ()
    allowed_effects: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            raise InvalidInput("tool scope requires a principal")
        if any(not root.strip() for root in self.workspace_roots):
            raise InvalidInput("tool scope roots must be non-empty")
        if any(not effect.strip() for effect in self.allowed_effects):
            raise InvalidInput("tool scope effects must be non-empty")


@dataclass(frozen=True)
class ToolPermission:
    effects: frozenset[str] = frozenset()
    approval: ApprovalMode = ApprovalMode.NOT_REQUIRED

    def __post_init__(self) -> None:
        if any(not effect.strip() for effect in self.effects):
            raise InvalidInput("tool permission effects must be non-empty")


@dataclass(frozen=True)
class ToolGatewayRequest:
    request_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    scope: ToolScope
    permission: ToolPermission
    deadline_monotonic: float | None = None
    cancellation: "CancellationSignal | None" = None
    approval_token: str | None = None

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.tool_name.strip():
            raise InvalidInput("tool request requires request_id and tool_name")
        if not isinstance(self.arguments, Mapping):
            raise InvalidInput("tool arguments must be a mapping")
        if self.deadline_monotonic is not None and self.deadline_monotonic <= 0:
            raise InvalidInput("deadline must be a positive monotonic timestamp")


class CancellationSignal(Protocol):
    @property
    def cancelled(self) -> bool: ...


class SchemaValidator(Protocol):
    def validate(self, tool_name: str, arguments: Mapping[str, Any]) -> None: ...


class PermissionEvaluator(Protocol):
    def authorize(self, tool_name: str, scope: ToolScope, permission: ToolPermission) -> None: ...


class ApprovalGate(Protocol):
    def approve(self, request: ToolGatewayRequest) -> bool: ...


class ToolInvoker(Protocol):
    def invoke(self, request: ToolGatewayRequest) -> "ToolInvocationOutput": ...


class OutputRedactor(Protocol):
    def redact(self, tool_name: str, output: Any) -> Any: ...


class ReceiptSink(Protocol):
    def record(self, receipt: "ToolReceipt") -> None: ...


@dataclass(frozen=True)
class ToolInvocationOutput:
    success: bool
    output: Any = ""
    error_code: str = ""
    error: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolReceipt:
    request_id: str
    tool_name: str
    success: bool
    output: Any = ""
    error_code: str = ""
    error: str = ""
    duration_ms: int = 0
    redaction_applied: bool = True
    approval_required: bool = False


class ToolGateway:
    """Execute one typed request through the complete cross-cutting seam."""

    def __init__(
        self,
        schema: SchemaValidator,
        permissions: PermissionEvaluator,
        approvals: ApprovalGate,
        invoker: ToolInvoker,
        redactor: OutputRedactor,
        receipts: ReceiptSink,
    ) -> None:
        self._schema = schema
        self._permissions = permissions
        self._approvals = approvals
        self._invoker = invoker
        self._redactor = redactor
        self._receipts = receipts

    def execute(self, request: ToolGatewayRequest) -> ToolReceipt:
        started = time.monotonic()
        self._check_control(request)
        self._schema.validate(request.tool_name, request.arguments)
        self._permissions.authorize(request.tool_name, request.scope, request.permission)
        if request.permission.approval is ApprovalMode.REQUIRED:
            if not request.approval_token or not self._approvals.approve(request):
                raise Forbidden("tool approval is required")
        self._check_control(request)
        result = self._invoker.invoke(request)
        self._check_control(request)
        safe_output = self._redactor.redact(request.tool_name, result.output)
        receipt = ToolReceipt(
            request_id=request.request_id,
            tool_name=request.tool_name,
            success=result.success,
            output=safe_output,
            error_code=result.error_code,
            error=result.error,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            approval_required=request.permission.approval is ApprovalMode.REQUIRED,
        )
        self._receipts.record(receipt)
        return receipt

    @staticmethod
    def _check_control(request: ToolGatewayRequest) -> None:
        if request.deadline_monotonic is not None and time.monotonic() >= request.deadline_monotonic:
            raise DeadlineExceeded("tool gateway deadline exceeded")
        if request.cancellation is not None and request.cancellation.cancelled:
            raise Cancelled("tool gateway request cancelled")


__all__ = [
    "ApprovalGate", "ApprovalMode", "CancellationSignal", "OutputRedactor",
    "PermissionEvaluator", "ReceiptSink", "SchemaValidator", "ToolGateway",
    "ToolGatewayRequest", "ToolInvocationOutput", "ToolInvoker", "ToolPermission",
    "ToolReceipt", "ToolScope",
]
