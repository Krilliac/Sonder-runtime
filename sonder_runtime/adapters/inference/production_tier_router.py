"""Bind legacy tier suggestions to current production capability evidence.

The composition root supplies its live policy-derived tier map on every call.
This adapter owns store and identity I/O; it never imports the root server.
"""
from __future__ import annotations

import sonder_runtime.adapters.inference.capability_evidence as capability_evidence
import sonder_runtime.adapters.inference.ollama_endpoint as ollama_endpoint

from sonder_runtime.application.routing.identity_cache import IdentityObservationCache
from sonder_runtime.application.routing.request_capabilities import request_requirements

from ..provider_bindings import provider_bindings_from_env

_identity_cache = IdentityObservationCache()


def route(prompt, available_tiers=None, *, router, tier_models, request_payload=None, **route_options):
    """Keep lexical/semantic choice unless a requested capability measured a failure."""
    mode = capability_evidence.capability_routing_mode()
    available = list(tier_models) if available_tiers is None else list(available_tiers)
    if mode == "off":
        return router(prompt, available, capability_routing=mode, **route_options)
    required = request_requirements(
        request_payload, prompt=prompt,
        **{name: route_options[name] for name in (
            "tools", "structured_output", "has_image", "approx_tokens", "long_context",
        ) if name in route_options},
    ) | frozenset(route_options.get("required_capabilities") or ())
    if not required:
        return router(prompt, available, capability_routing=mode, **route_options)

    evidence = capability_evidence.load_production_evidence()
    bindings = provider_bindings_from_env()
    # Non-Ollama/cloud routes cannot inherit a same-named local model's evidence.
    models = {
        tier: model if (
            not tier.startswith("cloud-") and ":cloud" not in model.lower()
            and bindings.tier_providers.get(tier, bindings.default_generation_provider) == "ollama"
        ) else None
        for tier, model in tier_models.items()
    }

    def identity_for(model):
        if mode != "strict" and not evidence.has_fresh_failure("ollama", model, required):
            return None
        origin = ollama_endpoint.normalize()
        key = ("ollama", origin, model, capability_evidence.identity_context_tokens(request_payload))
        return _identity_cache.observe(
            key, lambda: capability_evidence.ollama_identity(origin, model, request_payload),
            evidence=evidence,
        )

    return router(
        prompt, available, recent_evidence=evidence, tier_models=models,
        identity_for=identity_for, request_payload=request_payload,
        capability_routing=mode, **route_options,
    )
