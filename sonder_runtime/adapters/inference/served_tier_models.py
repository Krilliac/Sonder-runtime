"""Name each local tier by the model that actually serves it.

The Sonder Inference gateway ignores the Ollama policy model and sends its
own (``SONDER_INFERENCE_TIER_MODELS``, else ``SONDER_INFERENCE_MODEL``), so a
label built from the policy map alone misreports what answered a tier bound
to Sonder Inference.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from ..provider_bindings import provider_bindings_from_env
from .sonder_inference_gateway import config_from_env


_OBSERVED: ContextVar[dict[str, str] | None] = ContextVar(
    "sonder_execution_route_served_models", default=None,
)


@contextmanager
def observation_scope():
    """Keep successful provider outcomes within one routed work request."""
    token = _OBSERVED.set({})
    try:
        yield
    finally:
        _OBSERVED.reset(token)


def record_served_model(tier: str, model: str, provider: str) -> None:
    """Remember the last successful generation on a tier, when scoped."""
    observed = _OBSERVED.get()
    if observed is not None and isinstance(model, str) and model.strip():
        # Mutate in place: a nested context (copy_context, asyncio task) shares
        # the scope's dict, so the outcome is visible where the header is built.
        observed[str(tier)] = "%s (%s)" % (model, provider)


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
    """Label Inference tiers by configuration or a scoped successful response.

    If bindings cannot be read, keep policy labels except for successful calls
    observed in this routed request. Labeling must never fail the caller.
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
        served = dict(tiers)
    observed = _OBSERVED.get()
    if observed:
        served.update({tier: label for tier, label in observed.items() if tier in served})
    return served
