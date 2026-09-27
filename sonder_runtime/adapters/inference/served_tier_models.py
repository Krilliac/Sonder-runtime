"""Name each local tier by the model that actually serves it.

The Sonder Inference gateway ignores the Ollama policy model and sends its
own (``SONDER_INFERENCE_TIER_MODELS``, else ``SONDER_INFERENCE_MODEL``), so a
label built from the policy map alone misreports what answered a tier bound
to Sonder Inference.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..provider_bindings import provider_bindings_from_env
from .sonder_inference_gateway import config_from_env


def served_prompt_model(
    model: str, tier: object, provider: object, env: Mapping[str, str] | None = None,
) -> str:
    """The bare model id to name in a rung's system prompt.

    ``provider`` is the rung's bound provider (``None`` for Ollama).  Only a
    Sonder Inference rung is renamed, to exactly what ``select_model`` sends
    for the tier label ``bind_rung`` gives the gateway; anything else, and any
    error reading the config, keeps ``model`` so the prompt never gets worse.
    """
    if provider != "sonder_inference":
        return model
    try:
        settings = config_from_env(env)
        served = settings.tier_models.get(str(tier or "sonder"), settings.model)
    except Exception:
        return model
    return str(served or "").strip() or model


def served_tier_models(tiers: Mapping[str, str], env: Mapping[str, str] | None = None) -> dict:
    """``tiers`` with each Sonder Inference tier named ``"<model> (sonder_inference)"``.

    Any error reading the bindings returns the policy map unchanged: this only
    labels output, so it must never fail the caller.
    """
    served = dict(tiers)
    try:
        bindings = provider_bindings_from_env(env)
        inference_tiers = [
            name for name, provider in bindings.tier_providers.items()
            if provider == "sonder_inference"
        ]
        if inference_tiers:
            settings = config_from_env(env)
            for name in inference_tiers:
                model = settings.tier_models.get(name, settings.model)
                served[name] = "%s (sonder_inference)" % model
    except Exception:
        return dict(tiers)
    return served
