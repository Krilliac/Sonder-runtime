"""Stable model-transport error adapter.

The concrete transport exception lives in the adapters layer because it
inherits from ``urllib.error.URLError``. Application code sees only the typed
``ModelGateway`` port and domain error taxonomy.
"""
from __future__ import annotations

import urllib.error
import math


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
        retry_after_seconds: float | None = None,
    ):
        self.kind = str(kind or "unknown")
        self.detail = str(detail or self.kind)[:800]
        self.transient = bool(transient)
        self.status = int(status) if status is not None else None
        self.attempts = max(0, int(attempts if attempts is not None else 1))
        self.cloud = bool(cloud)
        try:
            retry_after = float(retry_after_seconds)
        except (TypeError, ValueError):
            retry_after = None
        self.retry_after_seconds = (
            max(0.0, retry_after)
            if retry_after is not None and math.isfinite(retry_after) else None
        )
        super().__init__(self.detail)


ModelCallError.__module__ = __name__

__all__ = ["ModelCallError"]
