"""Endpoint-attributed Ollama reporting: error labels, inventories, fan-out size.

Every helper here keeps a printed address and the facts printed beside it
about the same endpoint. With a loopback primary plus remote pool members
(``SONDER_OLLAMA_WORKERS``), whole-pool facts used to be printed under the
primary's address: a remote worker's model list on the loopback line, and
"remote Ollama at 127.0.0.1:11434" on model errors.
"""
from __future__ import annotations

import time
from typing import Callable

import sonder_runtime.adapters.inference.ollama_endpoint as ollama_endpoint
from sonder_runtime.platform.runtime_threads import (
    ThreadPoolExecutor as owned_runtime_pool,
    run_bounded,
)

_SHOWN_MODELS = 8
# Pool members are probed concurrently under one overall budget, so a slow or
# unreachable member costs diagnostics a few seconds, not a 15 s read each.
INVENTORY_PARALLELISM = 8
INVENTORY_DEADLINE_SECONDS = 4.0


def error_endpoint(base: str, pool, display: str | None = None) -> dict:
    """``endpoint_loopback``/``display`` kwargs for a runtime model error.

    The label classifies the address actually printed (the primary), and pool
    membership is stated beside it, so a failure a remote member served is not
    passed off as purely local either.
    """
    display = ollama_endpoint.safe_display(base) if display is None else display
    if pool.has_remote_workers:
        display += " (worker pool also routes to remote workers)"
    return {"endpoint_loopback": ollama_endpoint.is_loopback(base), "display": display}


def model_list_summary(names) -> str:
    # Show the count AND an enumeration consistent with it: truncating the
    # list while printing the full count silently hid models (including
    # sonder:latest, the active tier). Cap the enumeration, say what was cut.
    names = list(names)
    shown = ", ".join(names[:_SHOWN_MODELS]) if names else "none"
    if len(names) > _SHOWN_MODELS:
        shown += ", +%d more" % (len(names) - _SHOWN_MODELS)
    return "ok (%d models: %s)" % (len(names), shown)


def model_inventory_lines(
    pool, *, get_tags: Callable[[], object], member_tags: Callable[[str], object],
    require_endpoint: Callable[[], None], names: Callable[[object], list],
    parallelism: int = INVENTORY_PARALLELISM,
    deadline_seconds: float = INVENTORY_DEADLINE_SECONDS,
) -> list[str]:
    """Diagnostics inventory lines, each attributed to the endpoint holding it.

    A pooled ``/api/tags`` read may be answered by any member, so with a pool
    every member is listed with its own catalog instead of one unattributed
    "ollama:" line. Members are read concurrently (at most ``parallelism`` at
    once) within one overall ``deadline_seconds``; a member that has not
    answered by then is reported as timed out and its late reply discarded.
    """
    if not pool.enabled:
        try:
            return ["  ollama: %s" % model_list_summary(names(get_tags()))]
        except Exception as e:
            return ["  ollama: ERROR %s" % e]
    origins = tuple(pool.origins)
    labels = ["  ollama @ %s:" % ollama_endpoint.safe_display(origin) for origin in origins]
    try:
        require_endpoint()
    except Exception as e:
        return ["%s ERROR %s" % (label, e) for label in labels]
    if not origins:
        return []
    deadline = time.monotonic() + float(deadline_seconds)

    def read(origin):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, None, False
        return run_bounded(
            lambda: model_list_summary(names(member_tags(origin))), remaining,
            name="sonder-diagnostics-tags",
        )

    with owned_runtime_pool(max_workers=max(1, min(int(parallelism), len(origins)))) as executor:
        outcomes = list(executor.map(read, origins))
    lines = []
    for label, (summary, error, completed) in zip(labels, outcomes, strict=True):
        if not completed:
            lines.append("%s ERROR timed out (no answer within %.1fs)" % (label, float(deadline_seconds)))
        elif error is not None:
            lines.append("%s ERROR %s" % (label, error))
        else:
            lines.append("%s %s" % (label, summary))
    return lines


def clamp_workers(
    requested: int, *, pool, model: str, cloud: bool, bridged: Callable[[], object],
) -> tuple[int, int | None]:
    """Clamp a generation fan-out to the pool's admission capacity for ``model``.

    Interactive pool admission waits only a short window for a slot, so
    candidates beyond capacity fail with "timed out waiting for Ollama worker
    capacity" instead of queueing. Returns ``(effective, capacity)``;
    ``capacity`` is None when no clamp applies (cloud, bridged provider, no
    pool, or no capability evidence yet).
    """
    if cloud or not pool.enabled:
        return requested, None
    try:
        if bridged() is not None:
            return requested, None
        capacity = pool.model_capacity(model)
    except Exception:
        return requested, None
    if capacity is None:
        return requested, None
    # Zero free slots still runs one candidate at a time rather than none.
    return max(1, min(requested, int(capacity))), int(capacity)


def workers_label(effective: int, requested: int, capacity: int | None) -> str:
    if effective == requested or capacity is None:
        return "workers=%d" % effective
    return "workers=%d, requested %d, clamped to available Ollama pool capacity %d" % (
        effective, requested, capacity)


__all__ = [
    "clamp_workers",
    "error_endpoint",
    "model_inventory_lines",
    "model_list_summary",
    "workers_label",
]
