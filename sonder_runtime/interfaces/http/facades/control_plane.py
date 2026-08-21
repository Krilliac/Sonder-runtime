"""HTTP-facing description for the authenticated control-plane snapshot route."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sonder_runtime.application.control_plane import (
    ControlPlaneProviderError,
)


class ControlPlaneSnapshotPort(Protocol):
    def snapshot(self, *, captured_at: str, revision: int = 0) -> Any: ...


@dataclass(frozen=True)
class ControlPlaneRoute:
    path: str
    requires_auth: bool = True
    media_type: str = "application/json"

    def render(
        self,
        service: ControlPlaneSnapshotPort,
        *,
        captured_at: str,
        revision: int = 0,
    ) -> tuple[int, dict[str, Any]]:
        try:
            snapshot = service.snapshot(captured_at=captured_at, revision=revision)
        except ControlPlaneProviderError:
            return 503, {"error": "control_plane_unavailable"}
        return 200, {"snapshot": snapshot.as_dict(), "digest": snapshot.digest()}


class ControlPlaneFacade:
    """Classify the single bounded operator snapshot route."""

    PATH = "/v1/admin/control-plane"

    def route(self, path: str) -> ControlPlaneRoute | None:
        normalized = str(path or "").split("?", 1)[0].rstrip("/") or "/"
        return ControlPlaneRoute(self.PATH) if normalized == self.PATH else None


__all__ = ["ControlPlaneFacade", "ControlPlaneRoute", "ControlPlaneSnapshotPort"]
