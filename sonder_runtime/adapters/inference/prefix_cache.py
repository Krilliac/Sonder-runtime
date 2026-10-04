"""Record the joined prefix-cache observation for a main-chat turn.

Thin adapter over ``application.prefix_cache_report``: normalizes the
provider's bounded response metadata, joins it with the logical manifest
decision, and publishes content-free metrics.  Fail-soft: cache accounting
must never break a conversation.
"""
from __future__ import annotations

import logging

from ...application.prefix_cache_report import (
    DEFAULT_MONITOR,
    ChatPrefixCacheMonitor,
    compose_local_system,
    prewarm_request,
)
from ...platform.metrics import default_registry
from .telemetry import from_ollama

logger = logging.getLogger(__name__)


def provider_label(*, cloud: bool, bridged: bool) -> str:
    if bridged:
        return "bridged"
    return "ollama_cloud" if cloud else "ollama"


def observe_chat_turn(system_text, *, model, cloud=False, bridged=False,
                      response_meta=None, monitor: ChatPrefixCacheMonitor | None = None):
    """Join and record one turn; returns the join, or ``None`` on any failure.

    ``bridged`` may be a zero-argument callable returning the active provider
    bridge rung (``None`` when generation stays on Ollama); it is resolved
    here so a failing probe cannot escape into the chat path.
    """
    try:
        if callable(bridged):
            bridged = bridged() is not None
        telemetry = (
            from_ollama(response_meta)
            if isinstance(response_meta, dict) and not bridged else None
        )
        join = (monitor or DEFAULT_MONITOR).observe(
            str(system_text or ""), model=str(model or ""),
            provider_id=provider_label(cloud=bool(cloud), bridged=bool(bridged)),
            telemetry=telemetry,
        )
        default_registry().observe_prefix_cache(join)
        logger.debug(f"prefix cache: model={model!r} {join.summary()}")
        return join
    except Exception:
        logger.debug("prefix cache observation failed", exc_info=True)
        return None


__all__ = [
    "compose_local_system", "observe_chat_turn", "prewarm_request", "provider_label",
]
