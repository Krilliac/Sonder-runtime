"""Explicit startup configuration normalization for the SPEC-5 runtime."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .provider_bindings import (
    TIER_PROVIDER_ENV,
    ProviderBindings,
    provider_bindings_from_env,
)


@dataclass(frozen=True)
class RuntimeConfig:
    """Validated, frozen configuration passed into the composition root."""

    profile: str = "workstation-local"
    model_backend: str = "ollama"
    sonder_home: str = ""
    provider_bindings: ProviderBindings | None = None


def build_config_from_env(
    profile: str,
    env: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Normalize startup environment once into an explicit config object.

    ``env`` is injectable for deterministic callers and tests; the default
    preserves the historical process-environment behavior.
    """

    source = os.environ if env is None else env
    binding_keys = (*TIER_PROVIDER_ENV.values(), "SONDER_EMBEDDING_PROVIDER")
    has_provider_overrides = any(
        str(source.get(key, "") or "").strip() for key in binding_keys
    )
    return RuntimeConfig(
        profile=profile,
        model_backend=(
            str(source.get("SONDER_MODEL_BACKEND", "") or "").strip().lower()
            or "ollama"
        ),
        sonder_home=source.get("SONDER_HOME", ""),
        provider_bindings=(
            provider_bindings_from_env(source) if has_provider_overrides else None
        ),
    )


__all__ = ["RuntimeConfig", "build_config_from_env"]
