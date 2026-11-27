"""Stable model-transport error adapter.

The concrete transport exception lives in the adapters layer because it
inherits from ``urllib.error.URLError``. Application code sees only the typed
``ModelGateway`` port and domain error taxonomy.
"""
from __future__ import annotations

import urllib.error
import math

from sonder_runtime.domain.model_usage import usage_count


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
        thinking_chars: int | None = None,
        reasoning_segments: int | None = None,
        served_origin: str | None = None,
        worker_id: str | None = None,
        pool_refusal: str | None = None,
    ) -> None:
        self.kind = str(kind or "unknown")
        self.detail = str(detail or self.kind)[:800]
        self.transient = bool(transient)
        self.status = int(status) if status is not None else None
        self.attempts = max(0, int(attempts if attempts is not None else 1))
        self.cloud = bool(cloud)
        try:
            retry_after = float(retry_after_seconds) if retry_after_seconds is not None else None
        except (TypeError, ValueError):
            retry_after = None
        self.retry_after_seconds = (
            max(0.0, retry_after)
            if retry_after is not None and math.isfinite(retry_after) else None
        )
        # These explicit scalar fields let a failed multi-segment reasoning
        # call retain bounded usage telemetry without attaching provider text
        # or private reasoning content to the exception.
        self.thinking_chars = usage_count(thinking_chars)
        self.reasoning_segments = usage_count(reasoning_segments)
        # The pool worker that served (and failed) the request, when known:
        # lets error text name the host that actually answered, not the primary.
        self.served_origin = str(served_origin)[:256] if served_origin else None
        self.worker_id = str(worker_id)[:64] if worker_id else None
        # Reply text for a refusal by Sonder's own worker pool (busy, draining,
        # no worker), so a client is not told Ollama itself was unreachable.
        self.pool_refusal = str(pool_refusal)[:300] if pool_refusal else None
        super().__init__(self.detail)

    def restamped(self, *, attempts: int, cloud: bool) -> "ModelCallError":
        """A non-transient copy carrying the caller's attempt count and route.

        The local retry loop re-issues every classified error it does not
        replay; subclasses that carry typed evidence override this to keep it.
        """
        return ModelCallError(
            self.kind, self.detail, transient=False, status=self.status,
            attempts=attempts, cloud=cloud, retry_after_seconds=self.retry_after_seconds,
            served_origin=self.served_origin, worker_id=self.worker_id,
            pool_refusal=self.pool_refusal,
        )


ModelCallError.__module__ = __name__

__all__ = ["ModelCallError"]
