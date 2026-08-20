"""HTTP boundary facades with no dependency on the legacy runtime root."""

from .health_status import HealthStatusFacade, HealthStatusRoute

__all__ = ["HealthStatusFacade", "HealthStatusRoute"]
