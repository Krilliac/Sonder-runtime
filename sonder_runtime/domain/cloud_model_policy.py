"""Pure policy for selecting a live cloud-model override."""
from __future__ import annotations

import logging
from collections.abc import Collection

logger = logging.getLogger(__name__)


DEFAULT_RETIRED_CLOUD_MODELS = frozenset({"qwen3-coder:480b-cloud"})


def live_cloud_model(
    configured: object,
    default: str,
    retired_models: Collection[str] = DEFAULT_RETIRED_CLOUD_MODELS,
) -> object:
    """Return a configured cloud model unless it is empty or retired.

    The comparison is deliberately case-insensitive, while a live override is
    returned with its original spelling/type for compatibility with operator config.
    ``retired_models`` is injectable so callers can project their own provider
    catalog without coupling this pure policy to process configuration.
    """
    lowered = str(configured or "").strip().lower()
    retired = {str(model).strip().lower() for model in retired_models}
    if not lowered or lowered in retired:
        if lowered and lowered in retired:
            logger.warning(f"configured cloud model {configured!r} is retired, falling back to default={default!r} -- update your configuration to suppress this warning")
            logger.info(f"cloud model {configured!r} is retired, falling back to default={default!r}")
        logger.debug(f"live_cloud_model: configured={configured!r} is empty or retired, using default={default!r}")
        return default
    logger.info(f"cloud model override active: {configured!r}")
    logger.debug(f"live_cloud_model: using configured model {configured!r}")
    return configured


__all__ = ["DEFAULT_RETIRED_CLOUD_MODELS", "live_cloud_model"]
