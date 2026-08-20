"""Configuration loading helpers for read-only bootstrap checks.

The bootstrap layer owns the intentionally defensive boundary used by health
surfaces: configuration may be unavailable or invalid, and callers that only
need an optional snapshot should receive ``None`` rather than an exception.
The typed configuration loader remains the owner of parsing and validation;
this module owns the bootstrap-facing diagnostic policy around that loader.
"""
from __future__ import annotations


def load_config_or_none():
    """Return the production configuration when available, otherwise ``None``."""
    try:
        from sonder_runtime.platform import config as sonder_config
    except Exception:
        return None
    try:
        return sonder_config.load_config()
    except Exception:
        return None


def check_config():
    """Return a read-only doctor check for the production configuration."""
    try:
        from sonder_runtime.platform import config as sonder_config
        from sonder_runtime.adapters.config_validation import (
            validated_config_check,
        )
    except Exception as exc:
        return lambda: {
            "status": "skipped",
            "detail": "sonder_config unavailable (%s)" % exc,
        }

    def check():
        try:
            config = sonder_config.load_config()
        except sonder_config.ConfigError as exc:
            return {"status": "fail", "detail": "config invalid: %s" % exc}
        except Exception as exc:
            return {"status": "skipped", "detail": "config load failed (%s)" % exc}
        return validated_config_check(config)()

    return check


__all__ = ["check_config", "load_config_or_none"]
