"""Provider-neutral model gateway coordination contracts."""

from .health_and_roles import (
    GatewayBudget,
    GatewayRoute,
    LogicalRole,
    ModelGatewayContract,
    ModelParameters,
    NpuBoundary,
    ProviderHealth,
    ProviderState,
    RoleBinding,
    RoleBudgetBook,
)
from .facade import ModelGatewayFacade
from .escalation import (
    ControlledEscalation,
    ControlledEscalationPolicy,
    ControlledEscalationService,
    EscalationDecision,
    EscalationOutcome,
    EscalationReason,
    EscalationRequest,
    EscalationRoute,
)

__all__ = [
    "GatewayBudget",
    "GatewayRoute",
    "LogicalRole",
    "ModelGatewayContract",
    "ModelParameters",
    "NpuBoundary",
    "ProviderHealth",
    "ProviderState",
    "RoleBinding",
    "RoleBudgetBook",
    "ControlledEscalation",
    "ControlledEscalationPolicy",
    "ControlledEscalationService",
    "EscalationDecision",
    "EscalationOutcome",
    "EscalationReason",
    "EscalationRequest",
    "EscalationRoute",
    "ModelGatewayFacade",
]
