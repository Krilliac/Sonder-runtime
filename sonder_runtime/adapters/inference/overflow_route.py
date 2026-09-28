"""Host wiring for the long-context overflow on the legacy chat loop.

``server.py`` passes itself as ``host`` so the overflow reads the live tier
map, policy cache, provider bindings and worker pool (a live reload or a test
patch of those names is seen here).  The decisions themselves live in
``application.routing.long_context_overflow``; nothing here may fail a turn.
"""
from __future__ import annotations

from ...application.routing import long_context_overflow as _overflow
from ...application.session.provider_attempts import report_route_overflow
from ...platform import context_policy
from ..observability import activity_tracker
from ..runtime_policy import long_context_overflow as _settings
from .overflow_availability import pool_model_availability


def plan(host, prompt, history, escalation_plan, explicit_pin=False):
    """Rebind a long turn's first rung to the overflow model; ``(plan, decision)``."""
    try:
        policy = host._RUNTIME_POLICY or host._refresh_runtime_policy(create=True)
        return _overflow.plan_turn(
            _settings(policy), escalation_plan, prompt=prompt, history=history,
            explicit_pin=explicit_pin,
            provider_for=lambda rung: host._bridge_provider_for_tier(rung.tier, rung.cloud),
            reasoning_model=host.TIERS.get("reasoning") or "",
            availability=lambda name: pool_model_availability(
                host.OLLAMA_POOL, name, local_has_model=host.resolve_discovered_model_record,
            ),
        )
    except Exception:
        return escalation_plan, None


def rung_started(rung, decision, req_ctx, pinned_ctx):
    """Announce an overflow attempt and size its window to hold the prompt."""
    if decision is None or not _overflow.is_overflow(rung):
        return req_ctx
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
