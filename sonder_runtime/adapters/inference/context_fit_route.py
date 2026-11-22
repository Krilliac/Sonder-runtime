"""Host wiring for context-length routing on the legacy HTTP chat loop.

``server.py`` passes itself as ``host`` (live tier map, provider bindings,
runtime-policy cache, Ollama metadata cache), exactly as for the long-context
overflow (``overflow_route``), which calls into this module.  The decision
itself is ``application.routing.context_fit``.

Cost on the normal path: a turn whose estimated prompt is at or below
``context_fit.PROBE_FLOOR_TOKENS`` does no lookup at all.  Above it, a Sonder
Inference tier reads the TTL-cached ``/v1/models`` catalog (at most one bounded
GET per TTL per endpoint), and an Ollama tier reads the same ``/api/show``
metadata the turn's automatic window reads next (cached by ``server``).
Nothing here may fail a turn except the one deliberate refusal: a request
known not to fit any configured tier.

Thread affinity: none.  Every function reads the host's live state on the
calling thread; the only shared state is the gateway's profile cache
(``launch_profile_cache``), which is lock-protected.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, cast

from ...application.routing import context_fit as _fit
from ...application.routing import long_context_overflow as _overflow
from ...application.routing import tier_escalation
from ...application.session.provider_attempts import report_route_overflow
from ...domain.inference_profiles import LaunchProfileCatalog
from ...domain.runtime_policy import rules as _rules
from ...platform import context_policy
from ...platform import tier_model_options
from ..model_transport import ModelCallError
from ..observability import activity_tracker
from ..provider_bindings import provider_bindings_from_env
import sonder_runtime.adapters.inference.launch_profile_cache as launch_profile_cache

# Tiers a too-long turn may be moved off.  ``sonder`` is the default local
# alias route; an exact model pin (any other label) is never rerouted.
_ELIGIBLE_REROUTE = frozenset(_rules.LOCAL_TIERS) | {"sonder"}
PROVIDER_OLLAMA = "ollama"
PROVIDER_INFERENCE = "sonder_inference"


def _record_activity(kind: str, **fields: object) -> None:
    # ``record_event`` is an untyped legacy helper; it is looked up per call
    # (so a test patch is seen) and called through a declared signature.
    cast(Callable[..., object], activity_tracker.record_event)(kind, **fields)


def _native_max() -> int:
    return int(cast(Callable[[], int], context_policy.native_max)())


def _inference_catalog(env: Mapping[str, str] | None, *, allow_fetch: bool) -> tuple[str, LaunchProfileCatalog | None, Any]:
    """``(default model, catalog or None, settings)`` for the configured Inference."""
    from .sonder_inference_gateway import SonderInferenceGateway, config_from_env

    settings = config_from_env(env)
    if allow_fetch:
        catalog: LaunchProfileCatalog | None = SonderInferenceGateway(env=env).model_profiles()
    else:
        catalog = launch_profile_cache.cached_catalog(settings.base_url)
    return settings.model, catalog, settings


def _inference_window(
    tier: str, *, allow_fetch: bool, env: Mapping[str, str] | None = None,
) -> _fit.TierWindow:
    """The window of a Sonder Inference tier from its launch-profile metadata."""
    default_model, catalog, settings = _inference_catalog(env, allow_fetch=allow_fetch)
    model = str(settings.tier_models.get(tier, default_model))
    found = catalog.lookup(model) if catalog is not None else None
    if found is None or found.context_length is None:
        return _fit.TierWindow(tier, model, PROVIDER_INFERENCE, None)
    return _fit.TierWindow(
        tier, model, PROVIDER_INFERENCE, found.context_length, _fit.SOURCE_PROFILE,
    )


def _cached_model_context(host: Any, model: str) -> object:
    """``server``'s cached Ollama context length for ``model``; never probes."""
    key = str(model or "").strip().casefold()
    try:
        lock = getattr(host, "_MODEL_CONTEXT_CACHE_LOCK", None)
        if lock is not None:
            with lock:
                entry = host._MODEL_CONTEXT_CACHE.get(key)
        else:
            entry = host._MODEL_CONTEXT_CACHE.get(key)
    except Exception:
        return None
    return entry[1] if isinstance(entry, tuple) and len(entry) >= 2 else None


def _ollama_window(host: Any, tier: str, model: str, *, pinned: bool = False) -> _fit.TierWindow:
    """An Ollama tier's window: its configured ``num_ctx``, else the model's context.

    Unpinned turns read the model's metadata through ``_model_context_metadata``,
    the same cached ``/api/show`` read the turn's automatic window makes next,
    so no new request is added.  A turn with a pinned window never sizes
    itself from metadata, so it reads only what is already cached.
    """
    configured = None if pinned else tier_model_options.tier_num_ctx(tier)
    if configured is not None:
        return _fit.TierWindow(tier, model, PROVIDER_OLLAMA, configured, _fit.SOURCE_TIER_OPTION)
    context_length: object = None
    if model and pinned:
        context_length = _cached_model_context(host, model)
    elif model:
        try:
            context_length, _parameters = host._model_context_metadata(model)
        except Exception:
            context_length = None
    if (
        not isinstance(context_length, int)
        or isinstance(context_length, bool)
        or context_length <= 0
    ):
        return _fit.TierWindow(tier, model, PROVIDER_OLLAMA, None)
    return _fit.TierWindow(tier, model, PROVIDER_OLLAMA, context_length, _fit.SOURCE_MODEL)


def tier_window(
    host: Any, tier: str, model: str, provider: str | None, *, pinned: bool = False,
) -> _fit.TierWindow:
    """The effective window of ``tier`` served by ``provider`` (None = Ollama)."""
    if provider == PROVIDER_INFERENCE:
        return _inference_window(tier, allow_fetch=True)
    if provider is None or provider == PROVIDER_OLLAMA:
        return _ollama_window(host, tier, model, pinned=pinned)
    # Other providers (OpenAI-compatible peers) publish no window here.
    return _fit.TierWindow(tier, model, str(provider), None)


def _evidence_failure(target: _fit.TierWindow) -> str:
    """A reason when capability evidence says ``target`` fails long context.

    Mirrors ``production_tier_router``: only local Ollama models have
    evidence; routing mode ``off`` ignores it.  A store read error is no
    evidence.
    """
    if target.provider != PROVIDER_OLLAMA or not target.model:
        return ""
    try:
        import sonder_runtime.adapters.inference.capability_evidence as capability_evidence
        from ...domain.routing.backend_conformance import BackendCapability

        if capability_evidence.capability_routing_mode() == "off":
            return ""
        evidence = capability_evidence.load_production_evidence()
        if evidence.has_fresh_failure(
            PROVIDER_OLLAMA, target.model, frozenset({BackendCapability.LONG_CONTEXT}),
        ):
            return "recent long-context failure recorded for %s" % target.model
    except Exception:
        return ""
    return ""


def plan(
    host: Any, prompt: str, history: Any, escalation_plan: tier_escalation.Plan,
    *, pinned: bool = False,
) -> tuple[tier_escalation.Plan, _fit.FitDecision | None]:
    """``(plan, decision)`` for a turn; ``decision`` is ``None`` when it fits or is unknown."""
    try:
        estimated = _overflow.estimate_prompt_tokens(prompt, history)
        if estimated <= _fit.PROBE_FLOOR_TOKENS:
            return escalation_plan, None
        start = escalation_plan.start
        if start.cloud:
            return escalation_plan, None
        provider = host._bridge_provider_for_tier(start.tier, start.cloud)
        window = tier_window(
            host, str(start.tier or ""), str(start.model or ""), provider, pinned=pinned,
        )
        policy = host._RUNTIME_POLICY or host._refresh_runtime_policy(create=True)
        long_tier = str(_rules.effective_long_context_tier(policy, os.environ)["tier"])
        if str(start.tier or "").strip().lower() not in _ELIGIBLE_REROUTE:
            long_tier = ""

        def window_for(tier: str) -> _fit.TierWindow:
            model = str(host.TIERS.get(tier) or "")
            return tier_window(
                host, tier, model, host._bridge_provider_for_tier(tier, False), pinned=pinned,
            )

        decision = _fit.decide(
            estimated, window, long_context_tier=long_tier, window_for=window_for,
            evidence_failure=_evidence_failure,
        )
    except Exception:
        return escalation_plan, None
    if decision is None or not decision.switched or decision.target is None:
        return escalation_plan, decision
    target = decision.target
    rung = tier_escalation.Rung(
        tier=target.tier, model=str(host.TIERS.get(target.tier) or target.model),
        cloud=False, augment=start.augment, route=_fit.ROUTE,
    )
    # One rung: stepping back to a tier that cannot hold the prompt would
    # truncate it silently, which is what this router exists to prevent.
    return replace(escalation_plan, rungs=(rung,)), decision


def is_context_fit(decision: object) -> bool:
    return isinstance(decision, _fit.FitDecision)


def tier_num_ctx(rung: Any, req_ctx: int | None, pinned_ctx: int | None) -> int | None:
    """An Ollama rung's configured per-tier window, unless the request pinned one.

    ``req_ctx`` is ``None`` for a non-Ollama rung and ``0`` for a cloud rung;
    both are returned unchanged.
    """
    if pinned_ctx is not None or not req_ctx or getattr(rung, "cloud", False):
        return req_ctx
    configured = tier_model_options.tier_num_ctx(getattr(rung, "tier", ""))
    return configured if configured is not None else req_ctx


def rung_started(
    rung: Any, decision: _fit.FitDecision, req_ctx: int | None, pinned_ctx: int | None,
) -> int | None:
    """Refuse a turn that fits nowhere; size a rerouted Ollama rung's window."""
    if decision.status == _fit.STATUS_REJECTED:
        if getattr(rung, "route", "") != _fit.ROUTE:
            raise ModelCallError("configuration", decision.error_text(), status=400)
        return req_ctx
    if getattr(rung, "route", "") != _fit.ROUTE:
        return req_ctx
    report_route_overflow(decision.telemetry())
    try:
        _record_activity("model_route", summary=decision.notice(), model=rung.model)
    except Exception:
        pass
    if pinned_ctx is not None or not req_ctx:
        # A pinned window is the caller's; a non-Ollama rung has none to size.
        return req_ctx
    target = decision.target
    ceiling = (target.context_tokens if target is not None else None) or _native_max()
    return _overflow.context_window(decision.estimated_tokens, req_ctx, ceiling=ceiling)


def finished(decision: _fit.FitDecision) -> _fit.FitDecision:
    """Record the decision for the receipt (``sonder_receipt.overflow``)."""
    _overflow.record(decision)
    return decision


def tier_context_lines(policy: object, env: Mapping[str, str] | None = None) -> list[str]:
    """Status lines: the long-context tier and each tier's effective context.

    Reads only configuration and caches (never a socket), because
    ``runtime_policy_status`` is graded read-only.
    """
    source = os.environ if env is None else env
    setting = _rules.effective_long_context_tier(policy, source)
    lines = ["context fit: long-context tier %s%s%s" % (
        setting["tier"] or "(unset: a request that does not fit its tier fails with an error)",
        " [from %s]" % setting["source"] if setting["source"] else "",
        " [ignored invalid %s]" % setting["error"] if setting["error"] else "",
    )]
    options, option_error = tier_model_options.configured(source)
    try:
        bindings = provider_bindings_from_env(source)
        providers = dict(bindings.tier_providers)
        default_provider = bindings.default_generation_provider
    except Exception:
        providers, default_provider = {}, PROVIDER_OLLAMA
    models = policy.get("local_models") if isinstance(policy, dict) else None
    models = models if isinstance(models, dict) else {}
    lines.append("tier context (effective, from cached metadata):")
    for tier in _rules.LOCAL_TIERS:
        provider = providers.get(tier, default_provider)
        if provider == PROVIDER_INFERENCE:
            try:
                default_model, catalog, settings = _inference_catalog(source, allow_fetch=False)
            except Exception as exc:
                lines.append("  %s: sonder_inference, unknown (%s)" % (tier, type(exc).__name__))
                continue
            model = str(settings.tier_models.get(tier, default_model))
            found = catalog.lookup(model) if catalog is not None else None
            caps = (
                "not reported" if found is None or found.capabilities is None
                else ", ".join(found.capabilities) or "none"
            )
            context = (
                "%d tokens (profile %s)" % (found.context_length, found.profile or model)
                if found is not None and found.context_length is not None
                else "unknown (no profile metadata cached; `sonder doctor` or provider status reads it)"
            )
            lines.append("  %s: sonder_inference %s, context %s, capabilities %s" % (
                tier, model, context, caps,
            ))
            continue
        model = str(models.get(tier) or "")
        if not model:
            lines.append("  %s: (unbound)" % tier)
            continue
        tier_opts = options.get(tier, {})
        context = (
            "%d tokens (tier num_ctx)" % tier_opts["num_ctx"] if "num_ctx" in tier_opts
            else "auto (sized per request from the model's metadata)"
        )
        lines.append("  %s: %s %s, context %s" % (tier, provider, model, context))
    if option_error:
        lines.append("  [ignored invalid %s]" % option_error)
    return lines


__all__ = [
    "PROVIDER_INFERENCE",
    "PROVIDER_OLLAMA",
    "finished",
    "is_context_fit",
    "plan",
    "rung_started",
    "tier_context_lines",
    "tier_num_ctx",
    "tier_window",
]
