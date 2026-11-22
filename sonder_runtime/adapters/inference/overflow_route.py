"""Host wiring for the long-context overflow on the legacy chat loop.

``server.py`` passes itself as ``host`` so the overflow reads the live tier
map, policy cache, provider bindings and worker pool (a live reload or a test
patch of those names is seen here).  The decisions themselves live in
``application.routing.long_context_overflow``; nothing here may fail a turn.

Context-fit routing (``context_fit_route``) runs through the same three
hooks: when the opt-in overflow did not switch a turn, ``plan`` checks that
the prompt fits the model it is about to be sent to.  Its one deliberate
refusal (a request known not to fit any configured tier) is raised from
``rung_started`` before any model call.
"""
from __future__ import annotations

from ...application.routing import long_context_overflow as _overflow
from ...application.session.provider_attempts import report_route_overflow
from ...platform import context_policy
from ..observability import activity_tracker
from ..runtime_policy import long_context_overflow as _settings
import sonder_runtime.adapters.inference.context_fit_route as _context_fit
from .overflow_availability import pool_model_availability


def plan(host, prompt, history, escalation_plan, explicit_pin=False, context_size=""):
    """Rebind a long turn's first rung; ``(plan, decision)``.

    The opt-in overflow is decided first; when it did not switch the turn,
    context-fit routing checks that the prompt fits the model it is about to
    be sent to.  ``context_size`` is the request's explicit window, if any.
    """
    try:
        policy = host._RUNTIME_POLICY or host._refresh_runtime_policy(create=True)
        result = _overflow.plan_turn(
            _settings(policy), escalation_plan, prompt=prompt, history=history,
            explicit_pin=explicit_pin,
            provider_for=lambda rung: host._bridge_provider_for_tier(rung.tier, rung.cloud),
            reasoning_model=host.TIERS.get("reasoning") or "",
            availability=lambda name: pool_model_availability(
                host.OLLAMA_POOL, name, local_has_model=host.resolve_discovered_model_record,
            ),
        )
    except Exception:
        result = (escalation_plan, None)
    if result[1] is not None and result[1].switched:
        return result
    fitted, decision = _context_fit.plan(
        host, prompt, history, result[0], pinned=bool(str(context_size or "").strip()),
    )
    return (fitted, decision) if decision is not None else result


def rung_started(rung, decision, req_ctx, pinned_ctx):
    """Announce an overflow attempt and size its window to hold the prompt.

    Also applies an Ollama rung's configured per-tier ``num_ctx`` and refuses
    (with an actionable error) a turn that context-fit routing found fits no
    configured tier.
    """
    if _context_fit.is_context_fit(decision):
        return _context_fit.rung_started(
            rung, decision, _context_fit.tier_num_ctx(rung, req_ctx, pinned_ctx), pinned_ctx,
        )
    if decision is None or not _overflow.is_overflow(rung):
        return _context_fit.tier_num_ctx(rung, req_ctx, pinned_ctx)
    report_route_overflow(decision.telemetry())
    try:
        activity_tracker.record_event("model_route", summary=decision.notice(), model=rung.model)
    except Exception:
        pass
    if pinned_ctx is not None:
        return req_ctx
    return _overflow.context_window(
        decision.estimated_tokens, req_ctx, ceiling=context_policy.native_max(),
    )


def finished(decision, answered, failure=""):
    """Record how the overflow turned out, for the receipt and activity."""
    if _context_fit.is_context_fit(decision):
        return _context_fit.finished(decision)
    settled = _overflow.settle(decision, answered, failure)
    if settled is not None and not settled.switched:
        try:
            activity_tracker.record_event(
                "model_route", summary=settled.notice(), model=getattr(answered, "model", ""),
            )
        except Exception:
            pass
    _overflow.record(settled)
    return settled


__all__ = ["finished", "plan", "rung_started"]
