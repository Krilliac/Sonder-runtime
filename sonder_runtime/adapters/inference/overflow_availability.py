"""Which Ollama worker can serve the long-context overflow model, from cache.

Reads the pool's cached worker snapshots only: no probe, no request, no
lock held beyond the pool's own snapshot copy.  A worker that has not
reported its models yet makes the answer ``unknown`` (the overflow rung is
tried and the turn falls back if the pool rejects it); no worker listing the
model while every worker has reported makes it ``unavailable`` with a reason.
"""
from __future__ import annotations

from typing import Callable

from ...application.routing.long_context_overflow import (
    AVAILABLE,
    UNAVAILABLE,
    UNKNOWN,
    Availability,
)

_UNREPORTED = frozenset({"unknown", "stale", "reconnecting", "saturated"})
_SERVING = frozenset({"ready", "stale", "saturated"})


def _key(name) -> str:
    text = str(name or "").strip().casefold()
    return text if ":" in text or not text else text + ":latest"


def pool_model_availability(
    pool, model: str, *, local_has_model: Callable[[str], bool] | None = None,
) -> Availability:
    """Availability of ``model`` on the Ollama worker pool behind ``pool``.

    Without a worker pool (a single Ollama endpoint) ``local_has_model``
    answers from the local model catalog instead.
    """
    wanted = _key(model)
    if not wanted:
        return Availability(UNAVAILABLE, reason="no overflow model configured")
    if pool is None or not getattr(pool, "enabled", False):
        if local_has_model is None:
            return Availability(UNAVAILABLE, reason="no Ollama worker pool is configured")
        try:
            present = bool(local_has_model(model))
        except Exception:
            present = False
        if present:
            return Availability(AVAILABLE, worker="local Ollama")
        return Availability(UNAVAILABLE, reason="%s is not installed on local Ollama" % model)
    try:
        snapshots = tuple(pool.snapshots())
    except Exception:
        return Availability(UNKNOWN, worker="the Ollama pool")
    serving = [
        snapshot.worker_id for snapshot in snapshots
        if snapshot.healthy and snapshot.state in _SERVING
        and any(_key(name) == wanted for name in snapshot.models)
    ]
    if serving:
        worker = serving[0] if len(serving) == 1 else "%s (+%d more)" % (
            serving[0], len(serving) - 1,
        )
        return Availability(AVAILABLE, worker=worker)
    if any(
        snapshot.healthy and snapshot.state in _UNREPORTED and not snapshot.models
        for snapshot in snapshots
    ):
        return Availability(UNKNOWN, worker="the Ollama pool")
    return Availability(
        UNAVAILABLE, reason="no eligible Ollama pool worker advertises %s" % model,
    )


__all__ = ["pool_model_availability"]
