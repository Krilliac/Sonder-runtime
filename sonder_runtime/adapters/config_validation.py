"""Adapters for read-only configuration validation checks."""
from __future__ import annotations
from typing import Callable


def validated_config_check(config: object) -> Callable[[], dict[str, str]]:
    """Return a doctor check bound to an already validated config object."""

    def check() -> dict[str, str]:
        detail = "ollama=%s" % getattr(
            getattr(config, "ollama", None), "url", "?"
        )
        return {"status": "ok", "detail": detail}

    return check


__all__ = ["validated_config_check"]
