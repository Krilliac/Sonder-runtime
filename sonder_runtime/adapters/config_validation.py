"""Adapters for read-only configuration validation checks."""
from __future__ import annotations


def validated_config_check(config):
    """Return a doctor check bound to an already validated config object."""

    def check():
        detail = "ollama=%s" % getattr(
            getattr(config, "ollama", None), "url", "?"
        )
        return {"status": "ok", "detail": detail}

    return check


__all__ = ["validated_config_check"]
