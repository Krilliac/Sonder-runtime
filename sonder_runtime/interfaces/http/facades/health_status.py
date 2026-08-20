"""Root-free facade for the bounded lifecycle health/status route family.

The HTTP adapter owns sockets, authentication, CORS, and response writing.
This facade owns only route classification and translation of the existing
lifecycle application port into an HTTP-facing response description.  It is
deliberately independent of the legacy ``server`` module and model routes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class LifecycleStatusPort(Protocol):
    """Application port consumed by the health/status facade."""

    def live_payload(self) -> dict[str, Any]: ...

    def ready_payload(self) -> tuple[int, dict[str, Any]]: ...

    def health_payload(self) -> dict[str, Any]: ...

    def version_payload(self) -> dict[str, Any]: ...

    @property
    def metrics(self) -> Any: ...


@dataclass(frozen=True)
class HealthStatusRoute:
    """A classified route and its minimal access policy."""

    path: str
    requires_auth: bool
    media_type: str = "application/json"

    def render(self, lifecycle: LifecycleStatusPort) -> tuple[int, Any]:
        """Render the route using only the injected lifecycle port."""
        if self.path == "/live":
            return 200, lifecycle.live_payload()
        if self.path == "/ready":
            return lifecycle.ready_payload()
        if self.path == "/health":
            return 200, lifecycle.health_payload()
        if self.path == "/version":
            return 200, lifecycle.version_payload()
        if self.path == "/metrics":
            return 200, lifecycle.metrics.render()
        raise ValueError("unsupported health/status route")


class HealthStatusFacade:
    """Classify and render lifecycle health/status routes."""

    _ROUTES = {
        "/live": False,
        "/ready": True,
        "/health": True,
        "/version": True,
        "/metrics": True,
    }

    def route(self, path: str) -> HealthStatusRoute | None:
        """Return a route description, or ``None`` for unrelated paths."""
        normalized = str(path or "").rstrip("/") or "/"
        requires_auth = self._ROUTES.get(normalized)
        if requires_auth is None:
            return None
        media_type = (
            "text/plain; version=0.0.4; charset=utf-8"
            if normalized == "/metrics"
            else "application/json"
        )
        return HealthStatusRoute(normalized, requires_auth, media_type)


__all__ = ["HealthStatusFacade", "HealthStatusRoute", "LifecycleStatusPort"]
