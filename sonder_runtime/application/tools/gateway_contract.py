"""Provider-neutral ToolService gateway contract.

This is an application seam, not a provider implementation.  It makes the
cross-cutting rules around a tool invocation explicit: typed input, scope and
permission checks, approval, deadline/cancellation checks, invocation,
output redaction, and receipt publication.  The collaborators are ports and
must not perform provider, network, filesystem, or process I/O here.

Every request that gets past schema validation ends in exactly one receipt,
whichever way it ends: the receipt's ``terminal`` names the outcome
(``completed``, ``failed``, ``cancelled``, ``deadline_exceeded`` or
``policy_denied``), and the early exits publish theirs before re-raising, so
the durable audit sees a refused or abandoned call as plainly as a finished
one.  This is the repository's first terminal-reason vocabulary; grow it
from here rather than inventing another.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol

from ...domain.common.errors import Cancelled, DeadlineExceeded, Forbidden, InvalidInput

# Where a request came from and what privilege it carries. These mirror the
# literals of ``application.context``: the scope is what a typed request knows
# about its caller, and the operation context an invoker derives from it must
# not invent either value.
SOURCES = frozenset({"http", "mcp", "repl", "worker", "system"})
AUTH_LEVELS = frozenset({"local", "user", "developer", "admin"})

COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
DEADLINE_EXCEEDED = "deadline_exceeded"
POLICY_DENIED = "policy_denied"
TERMINAL_STATES = (COMPLETED, FAILED, CANCELLED, DEADLINE_EXCEEDED, POLICY_DENIED)

# Receipts for a tool call never name a model: nothing in this seam generates.
NOT_A_MODEL_CALL = ""


class ApprovalMode(Enum):
    NOT_REQUIRED = "not_required"
    REQUIRED = "required"


@dataclass(frozen=True)
class ToolScope:
    """Request scope supplied by the caller; it is never widened by a tool."""

    principal_id: str
    workspace_roots: tuple[str, ...] = ()
    allowed_effects: frozenset[str] = frozenset()
    source: str = "repl"
    auth_level: str = "local"

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            raise InvalidInput("tool scope requires a principal")
        if any(not root.strip() for root in self.workspace_roots):
            raise InvalidInput("tool scope roots must be non-empty")
        if any(not effect.strip() for effect in self.allowed_effects):
            raise InvalidInput("tool scope effects must be non-empty")
        if self.source not in SOURCES:
            raise InvalidInput("tool scope source must be one of %s"
                               % ", ".join(sorted(SOURCES)))
        if self.auth_level not in AUTH_LEVELS:
            raise InvalidInput("tool scope auth_level must be one of %s"
                               % ", ".join(sorted(AUTH_LEVELS)))


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
    session_id: str | None = None
    project_id: str | None = None
    execution_world: str = ""

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.tool_name.strip():
            raise InvalidInput("tool request requires request_id and tool_name")
        if not isinstance(self.arguments, Mapping):
            raise InvalidInput("tool arguments must be a mapping")
        if self.deadline_monotonic is not None and self.deadline_monotonic <= 0:
            raise InvalidInput("deadline must be a positive monotonic timestamp")
        for name, value in (("session_id", self.session_id), ("project_id", self.project_id)):
            if value is not None and not value.strip():
                raise InvalidInput(f"{name} must be non-empty when supplied")
        if not isinstance(self.execution_world, str):
            raise InvalidInput("execution_world must be text")


class CancellationSignal(Protocol):
    @property
    def cancelled(self) -> bool: ...


class SchemaValidator(Protocol):
    def validate(self, tool_name: str, arguments: Mapping[str, Any]) -> None: ...


class PermissionEvaluator(Protocol):
    """Admit or refuse a call; may return text naming the policy that matched."""

    def authorize(self, tool_name: str, scope: ToolScope, permission: ToolPermission) -> Any: ...


class ApprovalGate(Protocol):
    def approve(self, request: ToolGatewayRequest) -> bool: ...


class ToolInvoker(Protocol):
    def invoke(self, request: ToolGatewayRequest) -> "ToolInvocationOutput": ...


class OutputRedactor(Protocol):
    def redact(self, tool_name: str, output: Any) -> Any: ...


@dataclass(frozen=True)
class RedactedOutput:
    """Output plus an honest indication that redaction actually occurred."""

    value: Any
    applied: bool


class ReceiptSink(Protocol):
    def record(self, receipt: "ToolReceipt") -> None: ...


class ToolAuditRepository(Protocol):
    """Durable, scope-preserving audit sink for already-redacted receipts."""

    def append(self, request: ToolGatewayRequest, receipt: "ToolReceipt") -> None: ...


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
    requester_id: str = ""
    argument_digest: str = ""
    result_digest: str = ""
    execution_world: str = ""
    schema_version: str = "tool-receipt-v1"
    policy_match: str = ""
    resource: Mapping[str, Any] = field(default_factory=dict)
    effects: tuple[str, ...] = ()
    model: str = NOT_A_MODEL_CALL
    terminal: str = COMPLETED
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.terminal not in TERMINAL_STATES:
            raise InvalidInput("tool receipt terminal must be one of %s"
                               % ", ".join(TERMINAL_STATES))


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def _terminal_for(exc: BaseException) -> str:
    if isinstance(exc, Cancelled):
        return CANCELLED
    if isinstance(exc, DeadlineExceeded):
        return DEADLINE_EXCEEDED
    return POLICY_DENIED


def _match_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


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
        audit: ToolAuditRepository | None = None,
    ) -> None:
        self._schema = schema
        self._permissions = permissions
        self._approvals = approvals
        self._invoker = invoker
        self._redactor = redactor
        self._receipts = receipts
        self._audit = audit

    @classmethod
    def from_typed_ports(
        cls,
        registry: Any,
        policy: Any,
        executor: Any,
        permissions: PermissionEvaluator,
        approvals: ApprovalGate,
        redactor: OutputRedactor,
        receipts: ReceiptSink,
        *,
        audit: ToolAuditRepository | None = None,
        context_factory: Any = None,
    ) -> "ToolGateway":
        """Compose the gateway over the typed registry/execution ports.

        The gateway's request controls intentionally remain in this class;
        the typed adapters are limited to descriptor validation and execution
        after those controls have admitted the call.
        """
        from .typed_gateway import PortBackedToolInvoker, RegistrySchemaValidator

        invoker_kwargs = {}
        if context_factory is not None:
            invoker_kwargs["context_factory"] = context_factory
        return cls(
            RegistrySchemaValidator(registry),
            permissions,
            approvals,
            PortBackedToolInvoker(registry, policy, executor, **invoker_kwargs),
            redactor,
            receipts,
            audit=audit,
        )

    def execute(self, request: ToolGatewayRequest) -> ToolReceipt:
        started = time.monotonic()
        policy_match = ""
        try:
            self._check_control(request)
            self._schema.validate(request.tool_name, request.arguments)
            unscoped_effects = request.permission.effects - request.scope.allowed_effects
            if unscoped_effects:
                raise Forbidden(
                    "tool permission exceeds the request scope: %s"
                    % ", ".join(sorted(unscoped_effects))
                )
            policy_match = _match_text(
                self._permissions.authorize(request.tool_name, request.scope, request.permission)
            )
            if request.permission.approval is ApprovalMode.REQUIRED:
                if not request.approval_token or not self._approvals.approve(request):
                    raise Forbidden("tool approval is required")
            self._check_control(request)
            result = self._invoker.invoke(request)
            self._check_control(request)
        except (Cancelled, DeadlineExceeded, Forbidden) as exc:
            # The early exits are outcomes too: publish the receipt that names
            # how the call ended, then let the surface answer its caller.
            self._publish(request, self._early_receipt(
                request, exc, started,
                policy_match or _match_text(getattr(exc, "policy_match", "")),
            ))
            raise
        redacted = self._redactor.redact(request.tool_name, result.output)
        if isinstance(redacted, RedactedOutput):
            safe_output, redaction_applied = redacted.value, redacted.applied
        else:
            safe_output, redaction_applied = redacted, True
        evidence = dict((getattr(result, "metadata", None) or {}).get("evidence") or {})
        safe_evidence = self._redactor.redact(request.tool_name, evidence) if evidence else {}
        if isinstance(safe_evidence, RedactedOutput):
            safe_evidence = safe_evidence.value
        receipt = ToolReceipt(
            request_id=request.request_id,
            tool_name=request.tool_name,
            success=result.success,
            output=safe_output,
            error_code=result.error_code,
            error=result.error,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            redaction_applied=redaction_applied,
            approval_required=request.permission.approval is ApprovalMode.REQUIRED,
            requester_id=request.scope.principal_id,
            argument_digest=_digest(dict(request.arguments)),
            result_digest=_digest(safe_output),
            execution_world=getattr(request, "execution_world", ""),
            policy_match=policy_match,
            resource=self._resource(request),
            effects=tuple(sorted(request.permission.effects)),
            model=NOT_A_MODEL_CALL,
            terminal=COMPLETED if result.success else FAILED,
            evidence=safe_evidence if isinstance(safe_evidence, Mapping) else {},
        )
        self._publish(request, receipt)
        return receipt

    def _early_receipt(self, request: ToolGatewayRequest, exc: BaseException,
                       started: float, policy_match: str) -> ToolReceipt:
        return ToolReceipt(
            request_id=request.request_id,
            tool_name=request.tool_name,
            success=False,
            output="",
            error_code=type(exc).__name__,
            error=str(exc),
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            redaction_applied=True,
            approval_required=request.permission.approval is ApprovalMode.REQUIRED,
            requester_id=request.scope.principal_id,
            argument_digest=_digest(dict(request.arguments)),
            result_digest=_digest(""),
            execution_world=getattr(request, "execution_world", ""),
            policy_match=policy_match,
            resource=self._resource(request),
            effects=tuple(sorted(request.permission.effects)),
            model=NOT_A_MODEL_CALL,
            terminal=_terminal_for(exc),
        )

    @staticmethod
    def _resource(request: ToolGatewayRequest) -> dict[str, Any]:
        return {
            "principal_id": request.scope.principal_id,
            "workspace_roots": tuple(request.scope.workspace_roots),
            "source": getattr(request.scope, "source", ""),
            "auth_level": getattr(request.scope, "auth_level", ""),
        }

    def _publish(self, request: ToolGatewayRequest, receipt: ToolReceipt) -> None:
        # Audit first: a receipt that cannot be made durable is not published
        # at all, which is the fail-closed posture the audit boundary promises.
        if self._audit is not None:
            self._audit.append(request, receipt)
        self._receipts.record(receipt)

    @staticmethod
    def _check_control(request: ToolGatewayRequest) -> None:
        if request.deadline_monotonic is not None and time.monotonic() >= request.deadline_monotonic:
            raise DeadlineExceeded("tool gateway deadline exceeded")
        if request.cancellation is not None and request.cancellation.cancelled:
            raise Cancelled("tool gateway request cancelled")


__all__ = [
    "ApprovalGate", "ApprovalMode", "AUTH_LEVELS", "CANCELLED", "COMPLETED",
    "CancellationSignal", "DEADLINE_EXCEEDED", "FAILED", "NOT_A_MODEL_CALL",
    "OutputRedactor", "POLICY_DENIED", "PermissionEvaluator", "ReceiptSink",
    "RedactedOutput", "SOURCES", "SchemaValidator", "TERMINAL_STATES",
    "ToolAuditRepository", "ToolGateway", "ToolGatewayRequest",
    "ToolInvocationOutput", "ToolInvoker", "ToolPermission", "ToolReceipt",
    "ToolScope",
]
