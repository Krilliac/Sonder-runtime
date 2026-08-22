"""Explicit startup configuration normalization for the SPEC-5 runtime."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeConfig:
    """Validated, frozen configuration passed into the composition root."""

    profile: str = "workstation-local"
    model_backend: str = "ollama"
    sonder_home: str = ""


def build_config_from_env(
    profile: str,
    env: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Normalize startup environment once into an explicit config object.

    ``env`` is injectable for deterministic callers and tests; the default
    preserves the historical process-environment behavior.
    """

    source = os.environ if env is None else env
    return RuntimeConfig(
        profile=profile,
        model_backend=source.get("SONDER_MODEL_BACKEND", "ollama").strip().lower(),
        sonder_home=source.get("SONDER_HOME", ""),
    )


__all__ = ["RuntimeConfig", "build_config_from_env"]
