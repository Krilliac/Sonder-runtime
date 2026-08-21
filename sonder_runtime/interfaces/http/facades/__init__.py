"""HTTP boundary facades with no dependency on the legacy runtime root."""

from .health_status import HealthStatusFacade, HealthStatusRoute
from .extensions import ExtensionHttpResult, dispatch_extension_route

__all__ = [
    "ExtensionHttpResult",
    "HealthStatusFacade",
    "HealthStatusRoute",
    "dispatch_extension_route",
]
