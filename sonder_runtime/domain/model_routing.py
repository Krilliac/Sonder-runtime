"""Pure model-name classification used by runtime routing."""

from __future__ import annotations

from collections.abc import Collection, Mapping


def is_cloud_model_name(model: str | None) -> bool:
    """Return whether an Ollama model name denotes a hosted/cloud model.

    This intentionally preserves the server boundary's historical lexical
    contract: hosted names contain ``-cloud`` or end in ``:cloud``.  It does
    not validate, normalize, or select a model.
    """
    name = (model or "").lower()
    return "-cloud" in name or name.endswith(":cloud")


def is_cloud_tier(
    tier: str,
    model: str | None = None,
    *,
    cloud_tiers: Collection[str] = (),
    tier_map: Mapping[str, str] | None = None,
) -> bool:
    """Return whether a tier routes to cloud infrastructure.

    Checks the tier name against the provided cloud-tier set, falling back
    to lexical model-name classification on the resolved model.
    """
    if tier in cloud_tiers:
        return True
    if model is None:
        model = (tier_map or {}).get(tier, "")
    return is_cloud_model_name(model)
