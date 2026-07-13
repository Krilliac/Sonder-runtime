"""Stable transport error types shared across hot-reloaded runtime code."""
from __future__ import annotations

import urllib.error


class ModelCallError(urllib.error.URLError):
    """Safe, classified failure from one logical model call.

    This class lives outside ``server.py`` so an atomic server refresh cannot
    change its identity while another HTTP or fleet thread is carrying an
    in-flight exception across a boundary.
    """

    def __init__(
        self,
        kind: str,
        detail: str,
        *,
        transient: bool = False,
        status: int | None = None,
        attempts: int = 1,
        cloud: bool = False,
    ):
        self.kind = str(kind or "unknown")
        self.detail = str(detail or self.kind)[:800]
        self.transient = bool(transient)
        self.status = int(status) if status is not None else None
        self.attempts = max(0, int(attempts if attempts is not None else 1))
        self.cloud = bool(cloud)
        super().__init__(self.detail)
