"""Immutable model and tier configuration projection.

The composition root still owns the live, mutable tier bindings because the
runtime policy can refresh them in-process.  This module owns only the
import-time configuration projection that seeds those bindings.  It is pure:
callers provide an environment mapping, which keeps the domain boundary free
of process and filesystem I/O.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeModelConfiguration:
    """Typed, immutable projection of the runtime's model/tier defaults."""

    stable_alias: str
    local_code_model: str
    default_cloud_code_model: str
    default_cloud_general_model: str
    cloud_extra_usage_fallback_model: str
    retired_cloud_models: frozenset[str]
    tier_bindings: tuple[tuple[str, str], ...]
    cloud_tiers: tuple[str, ...]

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str]
    ) -> "RuntimeModelConfiguration":
        stable_alias = "sonder:latest"
        default_cloud_code_model = "kimi-k2.7-code:cloud"
        default_cloud_general_model = "glm-5.2:cloud"
        retired_cloud_models = frozenset({"qwen3-coder:480b-cloud"})

        def live_cloud_model(configured: object, default: str) -> str:
            lowered = str(configured or "").strip().lower()
            if not lowered or lowered in retired_cloud_models:
                if lowered and lowered in retired_cloud_models:
                    logger.warning(f"configured cloud model {configured!r} is retired, falling back to default={default!r}")
                return default
            return str(configured)

        cloud_code = live_cloud_model(
            env.get("SONDER_CLOUD_CODE"), default_cloud_code_model
        )
        cloud_general = live_cloud_model(
            env.get("SONDER_CLOUD_GENERAL"), default_cloud_general_model
        )
        logger.info(
            f"model configuration loaded: stable_alias={stable_alias!r}, "
            f"cloud_code={cloud_code!r}, cloud_general={cloud_general!r}"
        )
        logger.debug(
            f"RuntimeModelConfiguration.from_environment: "
            f"cloud_code={cloud_code!r}, cloud_general={cloud_general!r}, "
            f"stable_alias={stable_alias!r}"
        )
        empty_local_tiers = []
        if not str(env.get("SONDER_REASONING", "")).strip():
            empty_local_tiers.append("reasoning")
        if not str(env.get("SONDER_VISION", "")).strip():
            empty_local_tiers.append("vision")
        if empty_local_tiers:
            logger.warning(f"local tiers with no model configured: {empty_local_tiers} -- requests needing these capabilities will fall back to general-purpose models")
        return cls(
            stable_alias=stable_alias,
            local_code_model=str(env.get("SONDER_CODE_LOCAL", stable_alias)),
            default_cloud_code_model=default_cloud_code_model,
            default_cloud_general_model=default_cloud_general_model,
            cloud_extra_usage_fallback_model=default_cloud_code_model,
            retired_cloud_models=retired_cloud_models,
            tier_bindings=(
                ("fast", str(env.get("SONDER_FAST", stable_alias))),
                ("code", str(env.get("SONDER_CODE", stable_alias))),
                ("general", str(env.get("SONDER_GENERAL", stable_alias))),
                ("reasoning", str(env.get("SONDER_REASONING", ""))),
                ("vision", str(env.get("SONDER_VISION", ""))),
                ("cloud-code", cloud_code),
                ("cloud-general", cloud_general),
            ),
            cloud_tiers=("cloud-code", "cloud-general"),
        )

    def tier_map(self) -> dict[str, str]:
        """Return a mutable compatibility seed for the composition root."""
        return dict(self.tier_bindings)


__all__ = ["RuntimeModelConfiguration"]
