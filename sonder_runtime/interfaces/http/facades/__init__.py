"""HTTP boundary facades with no dependency on the legacy runtime root."""

from .health_status import HealthStatusFacade, HealthStatusRoute
from .extensions import ExtensionHttpResult, dispatch_extension_route
from .observability import TraceHttpResult, dispatch_trace_route
from .control_plane import ControlPlaneFacade, ControlPlaneRoute
from .a2a import A2AAgentCardFacade, A2AAgentCardRoute

__all__ = [
    "ExtensionHttpResult",
    "HealthStatusFacade",
    "HealthStatusRoute",
    "dispatch_extension_route",
    "TraceHttpResult",
    "dispatch_trace_route",
    "ControlPlaneFacade",
    "ControlPlaneRoute",
    "A2AAgentCardFacade",
    "A2AAgentCardRoute",
]
