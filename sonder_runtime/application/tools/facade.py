"""Provider-neutral tool graph and production-boundary composition."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from ...domain.common.errors import Forbidden
from ...domain.security import redaction as _redaction
from ..ports.tool_execution import ToolExecutionResult, ToolExecutor
from ..ports.tool_registry import ToolRegistry, ToolSchemaSelection
from .gateway_contract import (
    ApprovalGate,
    OutputRedactor,
    PermissionEvaluator,
    ReceiptSink,
    ToolGateway,
    ToolGatewayRequest,
    ToolInvocationOutput,
    ToolPermission,
    ToolScope,
    ToolReceipt,
    RedactedOutput,
)
from .generated_catalogs import CatalogBundle, GeneratedCatalogs
from .resource_policy import ResourcePolicy, ResourceRequest

logger = logging.getLogger(__name__)


def _effect_name(effect: Any) -> str:
    return effect.name.lower() if isinstance(effect, Enum) else str(effect).strip()


class ResourcePolicyEvaluator:
    """Adapt the rich resource policy to the gateway's narrow permission port."""

    def __init__(self, policy: ResourcePolicy, registry: ToolRegistry | None = None) -> None:
        self.policy = policy
        self.registry = registry

    def authorize(self, tool_name: str, scope: ToolScope, permission: ToolPermission) -> str:
        requested_effects = frozenset(permission.effects)
        if self.registry is not None:
            descriptor = self.registry.get(tool_name)
            if descriptor is None:
                raise Forbidden(f"tool policy has no descriptor for {tool_name!r}")
            declared_effects = frozenset(_effect_name(effect) for effect in descriptor.effects)
            if requested_effects != declared_effects:
                raise Forbidden(
                    f"tool permission effects do not match descriptor for {tool_name!r}"
                )

        # A multi-effect tool must be admitted for every declared effect.  A
        # single arbitrary set iteration previously allowed a read rule to
        # authorize a tool that also wrote files or used the network.
        effects = tuple(sorted(requested_effects)) or ("",)
        matched = []
        for effect in effects:
            result = self.policy.evaluate(ResourceRequest(
                request_id=f"policy:{tool_name}:{effect or 'pure'}",
                tool=tool_name,
                workspace=scope.workspace_roots[0] if scope.workspace_roots else "",
                side_effect_class=effect,
            ))
            if not result.allowed:
                error = Forbidden(f"tool policy denied {tool_name!r}: {result.receipt.reason}")
                error.policy_match = "resource:%s" % result.receipt.matched_rule_id
                raise error
            if result.approval_required and permission.approval.value == "not_required":
                raise Forbidden(f"tool policy requires approval for {tool_name!r}")
            matched.append(result.receipt.matched_rule_id)
        return "resource:%s" % ",".join(sorted(set(matched)))


class ChainedPermissionEvaluator:
    """Every evaluator must admit the call; the receipt names each match.

    The resource policy says what the graph admits at all; a second evaluator
    (the runtime's permission modes, adapted) says what the operator's
    standing policy allows right now. Both have to agree, and the receipt's
    ``policy_match`` records what each of them matched.
    """

    def __init__(self, *evaluators: PermissionEvaluator) -> None:
        self._evaluators = tuple(evaluators)

    def authorize(self, tool_name: str, scope: ToolScope, permission: ToolPermission) -> str:
        matches = []
        for evaluator in self._evaluators:
            match = evaluator.authorize(tool_name, scope, permission)
            if isinstance(match, str) and match:
                matches.append(match)
        return ";".join(matches)

    def authorize_request(self, request: ToolGatewayRequest) -> str:
        """Give each evaluator the whole request when it can take one."""
        matches = []
        for evaluator in self._evaluators:
            whole = getattr(evaluator, "authorize_request", None)
            if callable(whole):
                match = whole(request)
            else:
                match = evaluator.authorize(request.tool_name, request.scope, request.permission)
            if isinstance(match, str) and match:
                matches.append(match)
        return ";".join(matches)


class DenyApprovalGate:
    """Safe default for a graph with no interactive approval authority."""

    def approve(self, request: ToolGatewayRequest) -> bool:
        del request
        return False


class IdentityRedactor:
    """Explicitly records that no redaction authority was configured."""

    def redact(self, tool_name: str, output: Any) -> Any:
        del tool_name
        return RedactedOutput(output, applied=False)


class PatternOutputRedactor:
    """Scrub credential shapes from every string in a tool output.

    Applies the canonical domain pattern set (`domain.security.redaction`)
    across a bounded structure walk. It cannot know live secret *values*
    (the application layer has no environment access); a composition root
    that can — e.g. the runtime container — should inject a ``redact``
    callable built from the platform redactor so value- and pattern-based
    scrubbing compose at one seam. Fails closed: if the walk itself fails,
    the entire output is replaced rather than passed through unexamined.
    """

    def __init__(self, redact=None) -> None:
        self._redact = redact

    def redact(self, tool_name: str, output: Any) -> Any:
        del tool_name
        try:
            return RedactedOutput(
                _redaction.redact_structure(output, self._redact), applied=True
            )
        except Exception:
            return RedactedOutput(_redaction.REDACTION_FAILED, applied=True)


class ReceiptStore:
    """Process-local receipt sink; durable audit remains an injected port."""

    def __init__(self) -> None:
        self._items: list[ToolReceipt] = []

    def record(self, receipt: ToolReceipt) -> None:
        self._items.append(receipt)

    @property
    def items(self) -> tuple[ToolReceipt, ...]:
        return tuple(self._items)


class FailClosedToolExecutor:
    """Runtime default until a real provider adapter is explicitly composed."""

    def execute(self, descriptor, call, context, execution_class) -> ToolExecutionResult:
        del descriptor, call, context, execution_class
        return ToolExecutionResult(
            tool_name="unknown",
            success=False,
            error_code="provider_unconfigured",
            error="tool provider is not configured",
        )


@dataclass(frozen=True, slots=True)
class ToolGraph:
    registry: ToolRegistry
    policy: ResourcePolicy
    gateway: ToolGateway
    catalogs: CatalogBundle
    receipts: ReceiptStore


class ToolApplicationFacade:
    """One model-facing tool boundary with derived catalogs and receipts."""

    def __init__(self, graph: ToolGraph) -> None:
        if not isinstance(graph, ToolGraph):
            raise TypeError("graph must be a ToolGraph")
        self._graph = graph
        self._observers: tuple[Any, ...] = ()
        self._observers_lock = threading.Lock()

    def add_receipt_observer(self, observer: Any) -> None:
        """Call ``observer(request, receipt)`` after every executed request.

        Observers read; they never change the receipt the caller gets. One
        that raises is logged by exception type and skipped, so an observer
        can never turn a finished call into a failure. Adding the same
        observer twice is a no-op.
        """
        if not callable(observer):
            raise TypeError("observer must be callable")
        with self._observers_lock:
            if observer not in self._observers:
                self._observers = self._observers + (observer,)

    def remove_receipt_observer(self, observer: Any) -> None:
        with self._observers_lock:
            self._observers = tuple(item for item in self._observers if item != observer)

    @property
    def graph(self) -> ToolGraph:
        return self._graph

    @property
    def gateway(self) -> ToolGateway:
        return self._graph.gateway

    @property
    def catalogs(self) -> CatalogBundle:
        return self._graph.catalogs

    @property
    def receipts(self) -> tuple[ToolReceipt, ...]:
        return self._graph.receipts.items

    def schema_selection(
        self,
        visible_names: Any,
        *,
        selection_id: str = "",
    ) -> ToolSchemaSelection:
        """Build the immutable visibility value used by one model turn.

        The registry remains the executable authority.  Rejecting names that
        are absent from this graph prevents a caller from presenting a model
        schema that the same graph cannot execute.
        """
        selection = ToolSchemaSelection(
            frozenset(visible_names), selection_id=selection_id
        )
        registered = {descriptor.name for descriptor in self._graph.registry.list_all()}
        unknown = sorted(selection.visible_names - registered)
        if unknown:
            raise ValueError("tool schema selection contains unknown tools: %s" % ", ".join(unknown))
        return selection

    def visible_tool_schemas(
        self, selection: ToolSchemaSelection
    ) -> tuple[Mapping[str, Any], ...]:
        """Return the bounded schemas visible for a single model turn."""
        if not isinstance(selection, ToolSchemaSelection):
            raise TypeError("selection must be a ToolSchemaSelection")
        return tuple(self.catalogs_for(selection).client.get("tools", ()))

    def execute(self, request: ToolGatewayRequest) -> ToolReceipt:
        receipt = self._graph.gateway.execute(request)
        for observer in self._observers:
            try:
                observer(request, receipt)
            except Exception as exc:  # noqa: BLE001 - an observer never fails the call
                logger.warning("tool receipt observer failed for %s: %s",
                               request.tool_name, type(exc).__name__)
        return receipt

    @classmethod
    def compose(
        cls,
        registry: ToolRegistry,
        executor: ToolExecutor | None = None,
        *,
        policy: ResourcePolicy | None = None,
        approvals: ApprovalGate | None = None,
        redactor: OutputRedactor | None = None,
        receipts: ReceiptStore | None = None,
        commands: tuple[Any, ...] = (),
        audit: Any = None,
        permissions: tuple[PermissionEvaluator, ...] = (),
        context_factory: Any = None,
    ) -> "ToolApplicationFacade":
        """Compose the graph.

        ``audit`` is the durable audit repository the gateway publishes every
        receipt to before the process-local store sees it; ``permissions``
        are evaluators consulted after the resource policy, in order;
        ``context_factory`` builds the operation context an executor runs
        under from the request (the default carries the request scope's
        source and privilege).
        """
        policy = policy or ResourcePolicy()
        receipts = receipts or ReceiptStore()
        evaluator: PermissionEvaluator = ResourcePolicyEvaluator(policy, registry)
        if permissions:
            evaluator = ChainedPermissionEvaluator(evaluator, *permissions)
        gateway = ToolGateway.from_typed_ports(
            registry,
            _FailClosedTypedPolicy(),
            executor or FailClosedToolExecutor(),
            evaluator,
            approvals or DenyApprovalGate(),
            redactor or IdentityRedactor(),
            receipts,
            audit=audit,
            context_factory=context_factory,
        )
        catalogs = GeneratedCatalogs.generate(registry, commands=commands)
        return cls(ToolGraph(registry, policy, gateway, catalogs, receipts))

    def catalogs_for(self, selection: ToolSchemaSelection) -> CatalogBundle:
        """Generate a selected projection without changing the base catalog."""
        if not isinstance(selection, ToolSchemaSelection):
            raise TypeError("selection must be a ToolSchemaSelection")
        return GeneratedCatalogs.generate(
            self._graph.registry,
            selection=selection,
        )


class _FailClosedTypedPolicy:
    """Keep typed execution-class selection conservative at this seam."""

    def authorize(self, descriptor, call, context) -> None:
        del descriptor, call, context

    def select_execution_class(self, descriptor):
        return descriptor.execution_class


__all__ = [
    "ChainedPermissionEvaluator", "DenyApprovalGate", "FailClosedToolExecutor",
    "IdentityRedactor", "PatternOutputRedactor", "ReceiptStore",
    "ResourcePolicyEvaluator", "ToolApplicationFacade", "ToolGraph",
]
