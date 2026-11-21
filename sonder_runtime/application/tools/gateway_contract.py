"""Provider-neutral ToolService gateway contract.

This is an application seam, not a provider implementation.  It makes the
cross-cutting rules around a tool invocation explicit: typed input, scope and
permission checks, approval, deadline/cancellation checks, invocation,
output redaction, and receipt publication.  The collaborators are ports and
must not perform provider, network, filesystem, or process I/O here.

A receipt's ``terminal`` names how the request ended (``completed``,
``failed``, ``cancelled``, ``deadline_exceeded`` or ``policy_denied``).  Most
exits that raise publish a receipt before re-raising -- a refusal, a
cancellation or deadline, a crash in admission or in the invoker, and a
failure to redact the invoker's outcome or build its receipt (published as a
withheld ``failed`` fault with an ``uncertain`` effect) -- so the durable audit
sees those calls as plainly as a finished one.  This is the repository's
first terminal-reason vocabulary; grow it from here rather than inventing
another.

These exits publish no receipt (publication itself failing is covered
below):

* An exception from the descriptor and trait lookup in ``execute`` (before
  admission starts).
* During effect-journal admission and settled replay, any failure other than
  the caller-facing exits (cancellation, deadline, refusal) -- a journal
  storage error, a divergent or halted replay, a settled receipt that does
  not match -- because a replayed request id may already be in the audit and
  a second receipt under it would make the chain ambiguous.
* Any failure after the outcome receipt was handed to publication (the audit
  or sink raising, or the journal completion after it), for the same reason:
  that receipt may already be durable, so no second one follows it.
* A ``BaseException`` that is not an ``Exception`` (interrupt, exit), at any
  stage.

A receipt keeps three facts apart.  ``terminal`` is how the operation ended.
``effect_certainty`` is what that says about the tool's effect: ``settled``
(the tool returned its outcome), ``not_started`` (the call ended before the
invoker was entered) or ``uncertain`` (the invoker was entered and raised, or
returned an outcome that could not be redacted or receipted, so nothing here
can say its effect did not happen; a bound effect journal keeps the intent
unresolved for reconciliation).  Persistence is implicit: a
receipt exists only once it is published, and a failed publication of a
failure receipt is logged and never replaces the original exception.

Exception text is caller-derived and may quote arguments, paths or OS text,
so it is never published raw.  The caller-facing exits (cancellation,
deadline, refusal) pass their message through the gateway redactor, bounded,
with ``redaction_applied`` saying what the redactor actually did; if
redaction fails only a constant summary is published, with a diagnostic id
that is also logged.  Any other exception is a fault, not a message for the
caller, and is published as the same constant summary without its text.
"""
from __future__ import annotations

import logging
import time
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, Protocol
from uuid import uuid4

from ...domain.common.errors import Cancelled, DeadlineExceeded, Forbidden, InvalidInput
from ...domain.security.redaction import REDACTION_FAILED
from ...domain.tools.builtin_traits import builtin_traits
from ...domain.tools.traits import ToolTraits
from ..execution import effect_journal, gateway_calls
from ..ports.tool_registry import ToolSchemaSelection
from .scheduling import ToolConcurrencyGate
from .receipt_digest import receipt_digest as _digest

_LOG = logging.getLogger(__name__)

# Where a request came from and what privilege it carries. These mirror the
# literals of ``application.context``: the scope is what a typed request knows
# about its caller, and the operation context an invoker derives from it must
# not invent either value.
SOURCES = frozenset({"http", "mcp", "repl", "worker", "system"})
AUTH_LEVELS = frozenset({"local", "user", "developer", "admin"})

# Who made the permission decision for a request. ``gateway``: the gateway's
# permission evaluator decides (the native MCP surface, any caller with no
# gate of its own). ``surface``: an in-process surface already decided at its
# own entry point -- the console after its prompt, the legacy MCP and HTTP
# gates, the agent gate -- and forwards the call; the gateway then records
# that fact instead of deciding a second time, which would refuse the very
# call a person just answered yes to. Only in-process code can build a scope,
# so a protocol caller cannot claim it.
GATES = frozenset({"gateway", "surface"})

COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
DEADLINE_EXCEEDED = "deadline_exceeded"
POLICY_DENIED = "policy_denied"
TERMINAL_STATES = (COMPLETED, FAILED, CANCELLED, DEADLINE_EXCEEDED, POLICY_DENIED)

# What a receipt can say about the tool's effect (see the module docstring).
EFFECT_SETTLED = "settled"
EFFECT_NOT_STARTED = "not_started"
EFFECT_UNCERTAIN = "uncertain"
EFFECT_CERTAINTIES = (EFFECT_SETTLED, EFFECT_NOT_STARTED, EFFECT_UNCERTAIN)

# Bound on the exception text a failure receipt carries, applied after
# redaction so a secret that straddles the cut is redacted whole.
MAX_EARLY_ERROR_CHARS = 2000
# DoS bound on the exit text handed to the redactor. Longer text is not
# redacted piecewise (a cut could split a secret); it counts as a redaction
# failure and only the constant summary is published.
MAX_EXIT_TEXT_REDACTION_CHARS = 64 * 1024

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
    gate: str = "gateway"

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            raise InvalidInput("tool scope requires a principal")
        if self.gate not in GATES:
            raise InvalidInput("tool scope gate must be one of %s" % ", ".join(sorted(GATES)))
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
    reconciliation: str = "manual"
    traits: ToolTraits | None = None

    def __post_init__(self) -> None:
        if self.traits is not None and not isinstance(self.traits, ToolTraits):
            raise InvalidInput("tool permission traits must be ToolTraits")
        if any(not effect.strip() for effect in self.effects):
            raise InvalidInput("tool permission effects must be non-empty")
        if self.reconciliation not in {"manual", "idempotent", "query"}:
            raise InvalidInput("tool reconciliation must be manual, idempotent, or query")


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
    # Immutable per-turn visibility.  The executable registry remains the
    # authority; this value only narrows which registered schemas may run.
    schema_selection: ToolSchemaSelection | None = None

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
        if self.schema_selection is not None and not isinstance(
            self.schema_selection, ToolSchemaSelection
        ):
            raise InvalidInput("schema_selection must be a ToolSchemaSelection")


class CancellationSignal(Protocol):
    @property
    def cancelled(self) -> bool: ...


class SchemaValidator(Protocol):
    def validate(
        self, tool_name: str, arguments: Mapping[str, Any], selection: Any = None
    ) -> None: ...


class PermissionEvaluator(Protocol):
    """Admit or refuse a call; may return text naming the policy that matched.

    An evaluator that also defines ``authorize_request(request)`` is given the
    whole request (arguments included) and is preferred by the gateway; the
    narrow form stays the protocol's floor.
    """

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
    effect_certainty: str = EFFECT_SETTLED

    def __post_init__(self) -> None:
        if self.terminal not in TERMINAL_STATES:
            raise InvalidInput("tool receipt terminal must be one of %s"
                               % ", ".join(TERMINAL_STATES))
        if self.effect_certainty not in EFFECT_CERTAINTIES:
            raise InvalidInput("tool receipt effect_certainty must be one of "
                               + ", ".join(EFFECT_CERTAINTIES))


# The exits whose message is written for the caller. Any other exception is
# a fault whose text is never published.
_CALLER_FACING = (Cancelled, DeadlineExceeded, Forbidden)


def _terminal_for(exc: BaseException) -> str:
    if isinstance(exc, Cancelled):
        return CANCELLED
    if isinstance(exc, DeadlineExceeded):
        return DEADLINE_EXCEEDED
    if isinstance(exc, Forbidden):
        return POLICY_DENIED
    return FAILED


def _withheld_summary(terminal: str, exc: BaseException) -> str:
    return f"tool call ended as {terminal} ({type(exc).__name__}); details withheld"


def _match_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _authorize(evaluator: Any, request: ToolGatewayRequest) -> Any:
    """Prefer the request-aware form when an evaluator offers it."""
    whole = getattr(evaluator, "authorize_request", None)
    if callable(whole):
        return whole(request)
    return evaluator.authorize(request.tool_name, request.scope, request.permission)


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
        registry: Any = None,
        concurrency_gate: ToolConcurrencyGate | None = None,
    ) -> None:
        self._schema = schema
        self._registry = registry
        # Admission by concurrency trait is opt-in. A gateway instance is
        # shared by every surface of a process (agents, native MCP, HTTP,
        # lanes), so a default process-wide exclusive gate would hold every
        # unrelated call behind any long UNKNOWN-concurrency call (test_run
        # and build_job wait up to 60 s by default). A scheduler that wants
        # trait-driven overlap control composes its own gate.
        if concurrency_gate is not None and not isinstance(concurrency_gate, ToolConcurrencyGate):
            raise TypeError("concurrency_gate must be a ToolConcurrencyGate or None")
        self._concurrency = concurrency_gate
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
        concurrency_gate: ToolConcurrencyGate | None = None,
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
            registry=registry,
            concurrency_gate=concurrency_gate,
        )

    def execute(self, request: ToolGatewayRequest) -> ToolReceipt:
        # A typed registry is host authority; request metadata can never
        # replace it. Legacy callers retain their effects compatibility map.
        descriptor = self._registry.get(request.tool_name) if self._registry is not None else None
        # A typed registry is the authority for composed gateways.  The
        # legacy gateway has no descriptor, so retain the host's existing
        # built-in read declarations (for example ``read_file``) while still
        # leaving unclassified empty-effect tools conservative/unknown.
        traits = (descriptor.traits if descriptor is not None else
                  builtin_traits(request.tool_name, request.permission.effects))
        request = replace(request, permission=replace(request.permission, traits=traits))
        return self._execute_admitted(request)

    def _execute_admitted(self, request: ToolGatewayRequest) -> ToolReceipt:
        started = time.monotonic()
        policy_match = ""
        journal_binding = effect_journal.current()
        journal_intent = None
        # Which part of the call an exception came from. A journal admission
        # or settled-replay failure keeps its original contract (only the
        # caller-facing exits publish), so a refused replay never adds a
        # second receipt under a request id the audit already holds.
        stage = "admission"
        try:
            self._check_control(request)
            if request.schema_selection is None:
                # Preserve the small legacy schema port for callers that do
                # not opt into per-turn visibility.
                self._schema.validate(request.tool_name, request.arguments)
            else:
                self._schema.validate(
                    request.tool_name, request.arguments, request.schema_selection
                )
            unscoped_effects = request.permission.effects - request.scope.allowed_effects
            if unscoped_effects:
                raise Forbidden(
                    "tool permission exceeds the request scope: %s"
                    % ", ".join(sorted(unscoped_effects))
                )
            policy_match = _match_text(_authorize(self._permissions, request))
            if request.permission.reconciliation == "idempotent" and not request.permission.traits.replay_safe:
                raise Forbidden("tool replay requires host-declared idempotent=TRUE")
            if request.permission.approval is ApprovalMode.REQUIRED:
                if not request.approval_token or not self._approvals.approve(request):
                    raise Forbidden("tool approval is required")
            self._check_control(request)
            admission = (nullcontext() if self._concurrency is None else self._concurrency.admit(
                request.permission.traits.can_parallelize, lambda: self._check_control(request)
            ))
            with admission:
                if journal_binding is not None and (
                    request.permission.effects or not request.permission.traits.is_read_only
                ):
                    stage = "journal"
                    try:
                        journal_intent = self._begin_journaled(journal_binding, request)
                    except effect_journal.SettledEffectReplay as replay:
                        # A production child can consume its exact settled
                        # output without entering the invoker. Other callers
                        # keep the explicit replay-refusal contract.
                        calls = gateway_calls.current()
                        if calls is None or not callable(getattr(self._audit, "read_receipt", None)):
                            raise
                        from .settled_replay import consume_settled_receipt
                        try:
                            receipt = ToolReceipt(**consume_settled_receipt(
                                journal_binding, replay, request, self._audit,
                            ))
                            self._publish(request, receipt)
                        except BaseException as error:
                            # The ordinal matched, but the runner never got
                            # its output. It cannot checkpoint past this call
                            # or continue at a fresh ordinal after swallowing
                            # the refusal.
                            calls.halt(error)
                            raise
                        return receipt
                stage = "invoke"
                try:
                    result = self._invoker.invoke(request)
                except BaseException as exc:
                    if journal_binding is not None and journal_intent is not None:
                        try:
                            journal_binding.mark_uncertain(
                                journal_intent, detail=f"invoker raised {type(exc).__name__}"
                            )
                        except BaseException as uncertainty_error:  # noqa: BLE001 - preserve original interrupt
                            _LOG.error("tool effect uncertainty publication failed: %s",
                                       type(uncertainty_error).__name__)
                    raise
            # The executor returned the terminal outcome of an already admitted
            # effect. Preserve that truth even if cancellation/deadline arrived
            # in flight; the next invocation still fails its admission checks.
        except Exception as exc:
            # The exits that raise are outcomes too: publish the receipt that
            # names how the call ended, then let the surface answer its
            # caller. A BaseException that is not an Exception (interrupt,
            # exit) publishes nothing; the journal handling above still runs.
            if stage != "journal" or isinstance(exc, _CALLER_FACING):
                self._publish_failure(
                    request, exc, started,
                    policy_match or _match_text(getattr(exc, "policy_match", "")),
                    EFFECT_UNCERTAIN if stage == "invoke" else EFFECT_NOT_STARTED,
                )
            raise
        # Once the outcome receipt has been handed to ``_publish`` it may
        # already be in the audit, so no failure receipt may follow it.
        publish_attempted = False
        try:
            redacted = self._redactor.redact(request.tool_name, result.output)
            if isinstance(redacted, RedactedOutput):
                safe_output, redaction_applied = redacted.value, redacted.applied
            else:
                safe_output, redaction_applied = redacted, True
            # A failure message is shown and audited exactly like output, so it is
            # scrubbed the same way; a primitive's refusal can quote a path or a
            # value the caller sent.
            safe_error = self._redactor.redact(request.tool_name, result.error) if result.error else ""
            if isinstance(safe_error, RedactedOutput):
                safe_error = safe_error.value
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
                error=safe_error if isinstance(safe_error, str) else str(safe_error),
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
                terminal=(COMPLETED if result.success else CANCELLED
                          if result.error_code in {"TEST_CANCELLED", "CANCELLED", "Cancelled"}
                          else FAILED),
                evidence=safe_evidence if isinstance(safe_evidence, Mapping) else {},
            )
            # Durable audit publication is part of the effect receipt. Keep
            # the journal unresolved until it succeeds, so an audit failure
            # cannot certify an effect that a retry could repeat.
            publish_attempted = True
            self._publish(request, receipt)
            if journal_binding is not None and journal_intent is not None:
                journal_binding.complete(
                    journal_intent,
                    outcome_digest=receipt.result_digest,
                    receipt_key=receipt.request_id,
                    detail=receipt.error,
                    success=receipt.success,
                    # The redacted response the caller saw, so a mock replay
                    # can substitute it instead of re-invoking the tool.
                    response={
                        "success": receipt.success, "output": receipt.output,
                        "error_code": receipt.error_code, "error": receipt.error,
                    },
                )
        except BaseException as error:
            if journal_binding is not None and journal_intent is not None:
                try:
                    # The effect already ran. A failed redaction or receipt
                    # write cannot leave a reattachable bare intent. If the
                    # receipt committed before raising, the journal retains
                    # its definitive result instead of downgrading it.
                    journal_binding.mark_uncertain(
                        journal_intent, detail=f"post-invoke {type(error).__name__}"
                    )
                except Exception as recovery_error:  # noqa: BLE001 - preserve the original post-invoke failure
                    # Keep the original error; an unreadable journal still
                    # blocks safe checkpoint restore and blind replay.
                    _LOG.error("tool effect uncertainty publication failed: %s",
                               type(recovery_error).__name__)
            if not publish_attempted and isinstance(error, Exception):
                # The invoker ran but its outcome could not be redacted or
                # put into a receipt: the audit still records that the call
                # ended, as a fault whose text is withheld.
                self._publish_failure(request, error, started, policy_match,
                                      EFFECT_UNCERTAIN, fault=True)
            raise
        return receipt

    @staticmethod
    def _begin_journaled(journal_binding: effect_journal.JournalBinding,
                         request: ToolGatewayRequest) -> effect_journal.EffectIntent:
        """Commit the intent of a mutating call before it is invoked.

        A child runner bound by ``LocalSubagentProvider`` carries a gateway
        call sequence: the call is journaled under a deterministic identity
        (child run, worker, dispatch attempt, call ordinal and the canonical
        request digest), so a resumed runner that re-issues it meets the
        settled receipt (``SettledEffectReplay``) or, for a different
        request at that ordinal, ``DivergentEffectReplay``.  The sequence
        consumes an ordinal only for a durably admitted or settled call; any
        other admission failure halts it, so no later call of that runner
        runs.  Every other caller keeps its own ``request_id`` as the key.
        """
        calls = gateway_calls.current()
        if calls is None:
            return journal_binding.begin_request(
                operation_id=request.request_id,
                idempotency_key=request.request_id,
                request_digest=_digest(dict(request.arguments)),
                reconciliation=request.permission.reconciliation,
            )
        return calls.admit(
            journal_binding, tool_name=request.tool_name,
            arguments=request.arguments, effects=request.permission.effects or frozenset({"unknown"}),
            reconciliation=request.permission.reconciliation,
        )

    def _early_receipt(self, request: ToolGatewayRequest, exc: BaseException,
                       started: float, policy_match: str, *,
                       effect_certainty: str, fault: bool = False) -> ToolReceipt:
        # ``fault``: the exception is a gateway fault whatever its type (a
        # post-invoke redaction or receipt failure), never a caller exit.
        terminal = FAILED if fault else _terminal_for(exc)
        evidence: dict[str, Any] = {}
        if isinstance(exc, _CALLER_FACING) and not fault:
            error, redaction_applied, diagnostic_id = self._redact_exit_text(request, exc)
            if diagnostic_id:
                error = _withheld_summary(terminal, exc)
                evidence = {"error_redaction": "fallback", "diagnostic_id": diagnostic_id}
        else:
            # The text of a fault is unvetted; the type name is bounded.
            error, redaction_applied = _withheld_summary(terminal, exc), True
        return ToolReceipt(
            request_id=request.request_id,
            tool_name=request.tool_name,
            success=False,
            output="",
            error_code=type(exc).__name__,
            error=error,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            redaction_applied=redaction_applied,
            approval_required=request.permission.approval is ApprovalMode.REQUIRED,
            requester_id=request.scope.principal_id,
            argument_digest=_digest(dict(request.arguments)),
            result_digest=_digest(""),
            execution_world=getattr(request, "execution_world", ""),
            policy_match=policy_match,
            resource=self._resource(request),
            effects=tuple(sorted(request.permission.effects)),
            model=NOT_A_MODEL_CALL,
            terminal=terminal,
            evidence=evidence,
            effect_certainty=effect_certainty,
        )

    def _redact_exit_text(self, request: ToolGatewayRequest,
                          exc: BaseException) -> tuple[str, bool, str]:
        """Redact a caller-facing exit message: (text, applied, diagnostic id).

        A non-empty diagnostic id means redaction failed and the text must
        not be published; only the failure's type and the id are logged.
        The whole message is redacted before it is cut to
        ``MAX_EARLY_ERROR_CHARS``; one longer than
        ``MAX_EXIT_TEXT_REDACTION_CHARS`` is a redaction failure.
        """
        try:
            text = str(exc)
            if len(text) > MAX_EXIT_TEXT_REDACTION_CHARS:
                raise ValueError("exit text exceeds the redaction bound")
            redacted = self._redactor.redact(request.tool_name, text)
            if isinstance(redacted, RedactedOutput):
                value, applied = redacted.value, redacted.applied
            else:
                value, applied = redacted, True
            if not isinstance(value, str) or value == REDACTION_FAILED:
                raise TypeError("redactor returned no safe text")
            return value[:MAX_EARLY_ERROR_CHARS], bool(applied), ""
        except Exception as redaction_error:  # noqa: BLE001 - fall back to a constant summary
            diagnostic_id = uuid4().hex
            _LOG.warning("tool exit text redaction failed (%s); diagnostic_id=%s",
                         type(redaction_error).__name__, diagnostic_id)
            return "", True, diagnostic_id

    def _publish_failure(self, request: ToolGatewayRequest, exc: BaseException,
                         started: float, policy_match: str,
                         effect_certainty: str, *, fault: bool = False) -> None:
        """Build and publish the failure receipt without ever replacing ``exc``.

        A receipt that could not be built or published says nothing about the
        effect: the journal state set before this call stands. The error is
        logged by type and chained as the cause unless ``exc`` already has
        one.
        """
        try:
            self._publish(request, self._early_receipt(
                request, exc, started, policy_match, effect_certainty=effect_certainty,
                fault=fault,
            ))
        except Exception as publish_error:  # noqa: BLE001 - the original exit wins
            _LOG.error("tool failure receipt publication failed: %s",
                       type(publish_error).__name__)
            if exc.__cause__ is None:
                exc.__cause__ = publish_error

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
    "CancellationSignal", "DEADLINE_EXCEEDED", "EFFECT_CERTAINTIES",
    "EFFECT_NOT_STARTED", "EFFECT_SETTLED", "EFFECT_UNCERTAIN", "FAILED",
    "MAX_EARLY_ERROR_CHARS", "MAX_EXIT_TEXT_REDACTION_CHARS", "NOT_A_MODEL_CALL",
    "OutputRedactor", "POLICY_DENIED", "PermissionEvaluator", "ReceiptSink",
    "RedactedOutput", "SOURCES", "SchemaValidator", "TERMINAL_STATES",
    "ToolAuditRepository", "ToolGateway", "ToolGatewayRequest",
    "ToolInvocationOutput", "ToolInvoker", "ToolPermission", "ToolReceipt",
    "ToolScope",
]
