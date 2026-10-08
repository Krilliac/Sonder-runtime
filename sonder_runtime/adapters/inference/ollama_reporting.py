"""Endpoint-attributed Ollama reporting: error labels, inventories, fan-out size.

Every helper here keeps a printed address and the facts printed beside it
about the same endpoint. With a loopback primary plus remote pool members
(``SONDER_OLLAMA_WORKERS``), whole-pool facts used to be printed under the
primary's address: a remote worker's model list on the loopback line, and
"remote Ollama at 127.0.0.1:11434" on model errors.
"""
from __future__ import annotations

from typing import Callable

import sonder_runtime.adapters.inference.ollama_endpoint as ollama_endpoint

_SHOWN_MODELS = 8


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
) -> list[str]:
    """Diagnostics inventory lines, each attributed to the endpoint holding it.

    A pooled ``/api/tags`` read may be answered by any member, so with a pool
    every member is listed with its own catalog instead of one unattributed
    "ollama:" line.
    """
    if not pool.enabled:
        try:
            return ["  ollama: %s" % model_list_summary(names(get_tags()))]
        except Exception as e:
            return ["  ollama: ERROR %s" % e]
    lines = []
    for origin in pool.origins:
        label = "  ollama @ %s:" % ollama_endpoint.safe_display(origin)
        try:
            require_endpoint()
            lines.append("%s %s" % (label, model_list_summary(names(member_tags(origin)))))
        except Exception as e:
            lines.append("%s ERROR %s" % (label, e))
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
    if not capacity:
        return requested, None
    return max(1, min(requested, int(capacity))), int(capacity)


def workers_label(effective: int, requested: int, capacity: int | None) -> str:
    if effective == requested or capacity is None:
        return "workers=%d" % effective
    return "workers=%d, requested %d, clamped to Ollama pool capacity %d" % (
        effective, requested, capacity)


__all__ = [
    "clamp_workers",
    "error_endpoint",
    "model_inventory_lines",
    "model_list_summary",
    "workers_label",
]
