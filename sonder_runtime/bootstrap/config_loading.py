"""Configuration loading helpers for read-only bootstrap checks.

The bootstrap layer owns the intentionally defensive boundary used by health
surfaces: configuration may be unavailable or invalid, and callers that only
need an optional snapshot should receive ``None`` rather than an exception.
The typed configuration loader remains the owner of parsing and validation;
this module owns the bootstrap-facing diagnostic policy around that loader.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def load_config_or_none():
    """Return the production configuration when available, otherwise ``None``."""
    logger.debug("load_config_or_none: attempting config load")
    try:
        from sonder_runtime.platform import config as sonder_config
    except Exception as exc:
        logger.error(f"platform config module import failed, falling back to defaults", exc_info=True)
        logger.warning(f"configuration unavailable, falling back to defaults: platform config import failed ({type(exc).__name__})")
        logger.debug(f"load_config_or_none: platform config import failed: {exc}")
        return None
    try:
        cfg = sonder_config.load_config()
        logger.info(f"configuration loaded, profile={getattr(cfg, 'profile', 'unknown')!r}")
        logger.debug(f"load_config_or_none: config loaded, profile={getattr(cfg, 'profile', '?')!r}")
        return cfg
    except Exception as exc:
        logger.error(f"configuration load failed, falling back to defaults", exc_info=True)
        logger.warning(f"configuration unavailable, falling back to defaults: config load raised {type(exc).__name__}")
        logger.debug(f"load_config_or_none: config load raised: {exc}")
        return None


def check_config():
    """Return a read-only doctor check for the production configuration."""
    logger.debug("check_config: building config check callable")
    try:
        from sonder_runtime.platform import config as sonder_config
        from sonder_runtime.adapters.config_validation import (
            validated_config_check,
        )
    except Exception as exc:
        logger.error(f"config validation imports failed, doctor config check will be skipped", exc_info=True)
        logger.warning(f"config validation check unavailable, doctor will skip: {type(exc).__name__}")
        logger.debug(f"check_config: import failed, returning skipped: {exc}")
        return lambda: {
            "status": "skipped",
            "detail": "sonder_config unavailable (%s)" % exc,
        }

    def check():
        logger.debug("check_config: executing config validation")
        try:
            config = sonder_config.load_config()
        except sonder_config.ConfigError as exc:
            logger.error(f"configuration is invalid, doctor check reports failure", exc_info=True)
            logger.warning(f"configuration is invalid: {exc}")
            logger.debug(f"check_config: config invalid: {exc}")
            return {"status": "fail", "detail": "config invalid: %s" % exc}
        except Exception as exc:
            logger.error(f"configuration load failed during doctor check", exc_info=True)
            logger.warning(f"configuration load failed during doctor check: {type(exc).__name__}")
            logger.debug(f"check_config: config load failed: {exc}")
            return {"status": "skipped", "detail": "config load failed (%s)" % exc}
        result = validated_config_check(config)()
        logger.debug(f"check_config: result status={result.get('status', '?')!r}")
        return result

    return check


__all__ = ["check_config", "load_config_or_none"]
