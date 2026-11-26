"""Application service wiring operator policy, bound models, and health hints to execution_mode_router."""
from __future__ import annotations

import math
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any

from sonder_runtime.domain.routing.execution_mode_router import (
    DEFAULT_FAST_TIER_LATENCY_MS,
    DEFAULT_SLOW_TIER_LATENCY_MS,
    VALID_EXECUTION_MODES,
    ExecutionContext,
    ExecutionDecision,
    ExecutionModePolicy,
    ExecutionModeRequest,
    TierHealthHint,
    check_execution_mode,
    plan_execution_mode,
    route_summary_line,
)

ENV_AUTO_EXECUTION_MODE = "SONDER_AUTO_EXECUTION_MODE"
ENV_AUTO_ENSEMBLE_TIERS = "SONDER_AUTO_ENSEMBLE_TIERS"
ENV_SLOW_TIER_LATENCY_MS = "SONDER_ROUTER_SLOW_TIER_MS"
ENV_FAST_TIER_LATENCY_MS = "SONDER_ROUTER_FAST_TIER_MS"

_TEXT_TIERS: tuple[str, ...] = ("fast", "general", "code", "reasoning", "creative", "draft")


def _parse_positive_float(raw: Any, default: float) -> float:
    if raw is None or isinstance(raw, bool):
        return default
    text = str(raw).strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        return default
    if not math.isfinite(value) or value <= 0.0:
        return default
    return value


def _optional_float(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def normalize_tier_health(
    tier_health: Mapping[str, TierHealthHint | Mapping[str, Any]] | Sequence[TierHealthHint] | None = None,
    *,
    tier_latency_ms: Mapping[str, float] | None = None,
    unavailable_tiers: Collection[str] | None = None,
) -> tuple[TierHealthHint, ...]:
    """Normalize heterogeneous tier health/latency inputs into sorted domain hints."""
    by_tier: dict[str, TierHealthHint] = {}

    if isinstance(tier_health, Mapping):
        for raw_tier, raw_entry in tier_health.items():
            tier = str(raw_tier or "").strip().lower()
            if not tier:
                continue
            if isinstance(raw_entry, TierHealthHint):
                by_tier[tier] = TierHealthHint(
                    tier=tier,
                    available=bool(raw_entry.available),
                    healthy=bool(raw_entry.healthy),
                    latency_ms=raw_entry.latency_ms,
                    slow_threshold_ms=raw_entry.slow_threshold_ms,
                    error_rate=raw_entry.error_rate,
                    reason=raw_entry.reason,
                )
            elif isinstance(raw_entry, Mapping):
                err_val = _optional_float(raw_entry.get("error_rate"))
                by_tier[tier] = TierHealthHint(
                    tier=tier,
                    available=bool(raw_entry.get("available", True)),
                    healthy=bool(raw_entry.get("healthy", True)),
                    latency_ms=_optional_float(raw_entry.get("latency_ms")),
                    slow_threshold_ms=_optional_float(raw_entry.get("slow_threshold_ms")),
                    error_rate=max(0.0, err_val) if err_val is not None else 0.0,
                    reason=str(raw_entry.get("reason", "") or "").strip(),
                )
    elif tier_health is not None:
        for hint in tier_health:
            tier = (hint.tier or "").strip().lower()
            if not tier:
                continue
            by_tier[tier] = TierHealthHint(
                tier=tier,
                available=bool(hint.available),
                healthy=bool(hint.healthy),
                latency_ms=hint.latency_ms,
                slow_threshold_ms=hint.slow_threshold_ms,
                error_rate=hint.error_rate,
                reason=hint.reason,
            )

    if tier_latency_ms:
        for raw_tier, raw_latency in tier_latency_ms.items():
            tier = str(raw_tier or "").strip().lower()
            latency = _optional_float(raw_latency)
            if not tier or latency is None or latency < 0.0:
                continue
            existing = by_tier.get(tier)
            if existing is None:
                by_tier[tier] = TierHealthHint(tier=tier, latency_ms=latency)
            elif existing.latency_ms is None:
                by_tier[tier] = TierHealthHint(
                    tier=tier,
                    available=existing.available,
                    healthy=existing.healthy,
                    latency_ms=latency,
                    slow_threshold_ms=existing.slow_threshold_ms,
                    error_rate=existing.error_rate,
                    reason=existing.reason,
                )

    if unavailable_tiers:
        for raw_tier in unavailable_tiers:
            tier = str(raw_tier or "").strip().lower()
            if not tier:
                continue
            existing = by_tier.get(tier)
            if existing is None:
                by_tier[tier] = TierHealthHint(tier=tier, available=False, healthy=False)
            else:
                by_tier[tier] = TierHealthHint(
                    tier=tier,
                    available=False,
                    healthy=False,
                    latency_ms=existing.latency_ms,
                    slow_threshold_ms=existing.slow_threshold_ms,
                    error_rate=existing.error_rate,
                    reason=existing.reason,
                )

    return tuple(by_tier[tier] for tier in sorted(by_tier))


def policy_from_env(env: Mapping[str, str] | None = None) -> ExecutionModePolicy:
    """Build an :class:`ExecutionModePolicy` from environment settings.

    Accepted ``SONDER_AUTO_EXECUTION_MODE`` values:
    - ``""`` / ``"off"`` / ``"0"`` / ``"false"`` (default): auto-routing off;
      per-request ``execution_mode`` overrides are still honored.
    - ``"auto"`` / ``"1"`` / ``"true"`` / ``"on"``: enables automatic routing to
      ``fast`` (quick), ``ensemble`` (hard), and ``fanout`` (read-only multi-scope),
      while keeping mutating ``agent`` opt-in.
    - ``"auto+agent"``: enables ``auto`` plus automatic ``/agent`` dispatch for
      multi-scope implementation/verification tasks (still gated by developer auth
      and active permission/approval modes).
    - ``"single"`` / ``"ensemble"`` / ``"fanout"`` / ``"agent"``: pins default mode.
    """
    source: Mapping[str, str] = {} if env is None else env
    raw_mode = str(source.get(ENV_AUTO_EXECUTION_MODE, "") or "").strip().lower()
    allow_auto_agent = False
    mode: ExecutionModeRequest = "off"
    if raw_mode in ("", "off", "0", "false", "no", "disabled", "none"):
        mode = "off"
    elif raw_mode in ("auto", "1", "true", "yes", "on"):
        mode = "auto"
    elif raw_mode in ("auto+agent", "auto_agent", "full"):
        mode = "auto"
        allow_auto_agent = True
    elif raw_mode == "single":
        mode = "single"
    elif raw_mode == "ensemble":
        mode = "ensemble"
    elif raw_mode == "fanout":
        mode = "fanout"
    elif raw_mode == "agent":
        mode = "agent"

    raw_tiers = str(source.get(ENV_AUTO_ENSEMBLE_TIERS, "") or "").strip()
    tiers = tuple(
        t.strip().lower() for t in raw_tiers.split(",") if t.strip()
    ) or ("code", "reasoning")
    slow_ms = _parse_positive_float(
        source.get(ENV_SLOW_TIER_LATENCY_MS), DEFAULT_SLOW_TIER_LATENCY_MS
    )
    fast_ms = _parse_positive_float(
        source.get(ENV_FAST_TIER_LATENCY_MS), DEFAULT_FAST_TIER_LATENCY_MS
    )
    return ExecutionModePolicy(
        mode=mode,
        allow_auto_ensemble=True,
        allow_auto_fanout=True,
        allow_auto_agent=allow_auto_agent,
        ensemble_tiers=tiers,
        fanout_scope="local",
        slow_tier_latency_ms=slow_ms,
        fast_tier_latency_ms=fast_ms,
    )


def plan_chat_execution_mode(
    prompt: str,
    *,
    tier_models: Mapping[str, str] | None = None,
    resolve_tier_model: Callable[[str], str] | None = None,
    developer_authorized: bool = False,
    has_image: bool = False,
    structured_output: bool = False,
    explicit_model_pin: bool = False,
    requested_mode: str = "",
    conversation_depth: int = 0,
    tier_health: Mapping[str, TierHealthHint | Mapping[str, Any]] | Sequence[TierHealthHint] | None = None,
    tier_latency_ms: Mapping[str, float] | None = None,
    unavailable_tiers: Collection[str] | None = None,
    policy: ExecutionModePolicy | None = None,
    env: Mapping[str, str] | None = None,
) -> ExecutionDecision:
    """Resolve local tier capacity and health hints, then compute the execution-mode decision."""
    active_policy = policy if policy is not None else policy_from_env(env)
    resolved: dict[str, str] = {}
    if tier_models:
        for tier, model in tier_models.items():
            t = str(tier or "").strip().lower()
            m = str(model or "").strip()
            if t in _TEXT_TIERS and m:
                resolved[t] = m
    if resolve_tier_model is not None:
        for tier in _TEXT_TIERS:
            if tier in resolved:
                continue
            try:
                m = str(resolve_tier_model(tier) or "").strip()
            except Exception:
                m = ""
            if m:
                resolved[tier] = m

    ensemble_models = {
        resolved[t].casefold()
        for t in active_policy.ensemble_tiers
        if t in resolved and resolved[t]
    }
    all_local_models = {m.casefold() for m in resolved.values() if m}
    if len(ensemble_models) < 2 and len(all_local_models) >= 2:
        # If configured tiers collapsed onto one model, still allow distinct bound
        # local text models when available.
        ensemble_models = set(all_local_models)

    normalized_health = normalize_tier_health(
        tier_health,
        tier_latency_ms=tier_latency_ms,
        unavailable_tiers=unavailable_tiers,
    )

    ctx = ExecutionContext(
        prompt=prompt,
        available_tiers=frozenset(resolved.keys()),
        distinct_ensemble_models=len(ensemble_models),
        distinct_local_models=len(all_local_models),
        developer_authorized=developer_authorized,
        has_image=has_image,
        structured_output=structured_output,
        explicit_model_pin=explicit_model_pin,
        requested_mode=requested_mode,
        conversation_depth=conversation_depth,
        tier_health=normalized_health,
        tier_models=tuple(sorted(resolved.items())),
    )
    return plan_execution_mode(ctx, active_policy)


__all__ = [
    "ENV_AUTO_ENSEMBLE_TIERS",
    "ENV_AUTO_EXECUTION_MODE",
    "ENV_FAST_TIER_LATENCY_MS",
    "ENV_SLOW_TIER_LATENCY_MS",
    "VALID_EXECUTION_MODES",
    "TierHealthHint",
    "check_execution_mode",
    "normalize_tier_health",
    "plan_chat_execution_mode",
    "policy_from_env",
    "route_summary_line",
]
