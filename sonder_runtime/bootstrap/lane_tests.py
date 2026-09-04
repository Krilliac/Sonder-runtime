"""Supported opt-in composition for host-configured lane test targets."""

from dataclasses import dataclass
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
        permissions=(
            PermissionModesEvaluator(
                policy_names={**POLICY_NAMES, "run_tests": "workspace_run"}
            ),
        ),
        redactor=PatternOutputRedactor(Redactor().redact),
        receipts=ReceiptStore(),
        audit=audit,
        context_factory=_test_context,
    )
