"""Host-only adapter for independently approved catalog verification checks."""

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from ..application.ports.delegated_verification import (
    PreparedCheck,
    _PreparedCheckPermit,
)
from ..application.ports.tool_registry import ToolCall
from ..domain.tools.descriptors import ExecutionClass
from ..application.context import OperationContext


@dataclass(frozen=True)
class VerificationOperationContext(OperationContext):
    session_id: str | None = None


from .lane_tests import LaneTestExecutor, lane_test_descriptor


class CatalogVerificationGateway:
    def __init__(self, catalog, provider, *, targets=None):
        self.catalog, self.provider = catalog, provider
        self.executor = LaneTestExecutor(catalog, provider)
        self.targets = None if targets is None else MappingProxyType(dict(targets))
        self._issuer = None

    def bind_issuer(self, issuer):
        if self._issuer is not None:
            raise ValueError("verifier gateway is already bound")
        self._issuer = issuer

    def prepare_checks(self, roots):
        self.catalog.require_current()
        checks = []
        for root in roots:
            selected = self.targets.get(root) if self.targets is not None else None
            candidates = [
                t
                for t in self.catalog.targets.values()
                if str(t.workspace_root) == root
                and (selected is None or t.name == selected)
            ]
            if self.targets is not None and selected is None:
                candidates = []
            if len(candidates) != 1:
                raise ValueError("independent catalog target is missing or ambiguous")
            target = candidates[0]
            checks.append(
                PreparedCheck(
                    target.name,
                    self.catalog.digest,
                    target.argv_digest,
                    root,
                    target.argv,
                )
            )
        return tuple(checks)

    def require_current(self, checks):
        if checks != self.prepare_checks(tuple(c.workspace_root for c in checks)):
            raise PermissionError("approved catalog check binding changed")

    def execute_check(self, check, call_id, parent, context, *, permit):
        if (
            not isinstance(permit, _PreparedCheckPermit)
            or permit.issuer is not self._issuer
            or self._issuer is None
            or permit.check != check
            or permit.call_id != call_id
            or permit.prepared.parent_session_id != parent
            or permit.prepared.principal_id != context.principal_id
            or check not in permit.prepared.checks
            or not permit.approval_id
        ):
            raise PermissionError("exact prepared verification approval is required")
        self.require_current(permit.prepared.checks)
        if context.expired or context.cancellation.cancelled:
            raise PermissionError("verification authority ended before dispatch")
        values = vars(context).copy()
        values["workspace_roots"] = (Path(check.workspace_root),)
        values["session_id"] = parent
        execution_context = VerificationOperationContext(**values)
        return self.executor.execute(
            lane_test_descriptor(self.catalog),
            ToolCall("run_tests", {"target": check.target}, call_id),
            execution_context,
            ExecutionClass.HOST,
        )
