"""HTTP-facing description for the authenticated control-plane snapshot route."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

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
        logger.debug(f"ControlPlaneRoute.render: captured_at={captured_at!r}, revision={revision}")
        try:
            snapshot = service.snapshot(captured_at=captured_at, revision=revision)
        except ControlPlaneProviderError:
            logger.error("control plane provider unavailable, returning 503", exc_info=True)
            logger.debug("ControlPlaneRoute.render: provider error, returning 503")
            return 503, {"error": "control_plane_unavailable"}
        logger.debug(f"ControlPlaneRoute.render: snapshot digest={snapshot.digest()!r}")
        return 200, {"snapshot": snapshot.as_dict(), "digest": snapshot.digest()}


class ControlPlaneFacade:
    """Classify the single bounded operator snapshot route."""

    PATH = "/v1/admin/control-plane"

    def route(self, path: str) -> ControlPlaneRoute | None:
        normalized = str(path or "").split("?", 1)[0].rstrip("/") or "/"
        matched = normalized == self.PATH
        if matched:
            logger.debug(f"ControlPlaneFacade.route: matched path={normalized!r}")
        return ControlPlaneRoute(self.PATH) if matched else None


__all__ = ["ControlPlaneFacade", "ControlPlaneRoute", "ControlPlaneSnapshotPort"]
