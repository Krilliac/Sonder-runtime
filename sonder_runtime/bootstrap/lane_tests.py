"""Supported opt-in composition for host-configured lane test targets."""

from dataclasses import dataclass, replace
from ..adapters.lane_tests import (
    LaneTestCatalog,
    LaneTestExecutor,
    lane_test_descriptor,
)
from ..adapters.security.permission_evaluator import PermissionModesEvaluator
from ..application.context import OperationContext
from ..application.ports.tool_registry import InMemoryToolRegistry
from ..application.tools.facade import (
    ToolApplicationFacade,
    PatternOutputRedactor,
    ReceiptStore,
)
from ..application.tools.resource_policy import ResourcePolicy, PolicyRule, Decision
from ..application.tools.typed_gateway import default_tool_context
from ..platform.logging import Redactor
from .typed_tools import POLICY_NAMES


@dataclass(frozen=True)
class LaneTestOperationContext(OperationContext):
    session_id: str | None = None


def _test_context(request):
    original = default_tool_context(request)
    return LaneTestOperationContext(**vars(original), session_id=request.session_id)


class CatalogPermissionEvaluator(PermissionModesEvaluator):
    """Bind operator approval to the host-resolved command, not its short alias."""

    def __init__(self, catalog):
        super().__init__(policy_names={**POLICY_NAMES, "run_tests": "workspace_run"})
        self.catalog = catalog

    def authorize_request(self, request):
        if request.tool_name == "run_tests":
            self.catalog.require_current()
            target = self.catalog.targets[request.arguments["target"]]
            request = replace(
                request,
                arguments={
                    **request.arguments,
                    "configured_target": {
                        "argv": list(target.argv),
                        "argv_digest": target.argv_digest,
                        "workspace_root": str(target.workspace_root),
                        "catalog_digest": self.catalog.digest,
                    },
                },
            )
        return super().authorize_request(request)


def compose_lane_test_tools(base, catalog, process_provider, *, audit):
    """Add a fixed test catalog while retaining the runtime permission gate.

    Empty catalogs leave the original facade untouched. This host composition
    API cannot be invoked by a model tool; targets do not grant permission by
    themselves. The current operator policy also has to admit workspace_run.
    """
    if not isinstance(catalog, LaneTestCatalog):
        raise TypeError("catalog must be a validated LaneTestCatalog")
    if not catalog.targets:
        return base
    registry = InMemoryToolRegistry(
        [*base.graph.registry.list_all(), lane_test_descriptor(catalog)]
    )
    policy = ResourcePolicy(
        [
            *base.graph.policy.rules,
            PolicyRule(
                "configured-lane-tests",
                Decision.ALLOW,
                tool="run_tests",
                reason="exact host-configured test target; operator execution permission still required",
            ),
        ],
        authorities=base.graph.policy.authorities,
    )
    return ToolApplicationFacade.compose(
        registry,
        LaneTestExecutor(catalog, process_provider),
        policy=policy,
        permissions=(CatalogPermissionEvaluator(catalog),),
        redactor=PatternOutputRedactor(Redactor().redact),
        receipts=ReceiptStore(),
        audit=audit,
        context_factory=_test_context,
    )
