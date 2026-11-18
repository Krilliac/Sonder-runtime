"""Cached chat-fitness lookup for explicitly selected local models."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable

from sonder_runtime.domain.chat_model_fitness import unfit_reason

_TTL = 300.0
_FAILURE_TTL = 30.0


class ChatModelFitness:
    """Probe each local model's ``/api/show`` once per TTL.

    ``show`` returns the payload dict and may raise.  A failed probe is not
    evidence against the model: it reads as fit, and is retried after a short
    window.  Thread-safe; the lock is never held across the probe.
    """

    def __init__(
        self,
        show: Callable[[str], object],
        clock: Callable[[], float] = time.monotonic,
        *,
        vision_only_model_families: Iterable[str] = (),
    ) -> None:
        self._show = show
        self._clock = clock
        self._vision_only_model_families = tuple(vision_only_model_families)
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, float, str]] = {}

    def unfit_reason(self, model: str) -> str:
        key = str(model or "").strip().casefold()
        if not key:
            return ""
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < cached[1]:
                return cached[2]
        try:
            reason = unfit_reason(
                self._show(model),
                vision_only_model_families=self._vision_only_model_families,
            )
            ttl = _TTL
        except Exception:
            reason, ttl = "", _FAILURE_TTL
        with self._lock:
            self._cache[key] = (now, ttl, reason)
        return reason


__all__ = ["ChatModelFitness"]
