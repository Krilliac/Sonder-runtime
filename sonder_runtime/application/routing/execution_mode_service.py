"""Application service wiring operator policy and bound models to execution_mode_router."""
from __future__ import annotations

from collections.abc import Callable, Mapping

from sonder_runtime.domain.routing.execution_mode_router import (
    VALID_EXECUTION_MODES,
    ExecutionContext,
    ExecutionDecision,
    ExecutionModePolicy,
    ExecutionModeRequest,
    check_execution_mode,
    plan_execution_mode,
)

ENV_AUTO_EXECUTION_MODE = "SONDER_AUTO_EXECUTION_MODE"
ENV_AUTO_ENSEMBLE_TIERS = "SONDER_AUTO_ENSEMBLE_TIERS"

_TEXT_TIERS: tuple[str, ...] = ("fast", "general", "code", "reasoning", "creative", "draft")


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
    return ExecutionModePolicy(
        mode=mode,
        allow_auto_ensemble=True,
        allow_auto_fanout=True,
        allow_auto_agent=allow_auto_agent,
        ensemble_tiers=tiers,
        fanout_scope="local",
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
    policy: ExecutionModePolicy | None = None,
    env: Mapping[str, str] | None = None,
) -> ExecutionDecision:
    """Resolve local tier capacity and compute the execution-mode decision."""
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
    )
    return plan_execution_mode(ctx, active_policy)


__all__ = [
    "ENV_AUTO_ENSEMBLE_TIERS",
    "ENV_AUTO_EXECUTION_MODE",
    "VALID_EXECUTION_MODES",
    "check_execution_mode",
    "plan_chat_execution_mode",
    "policy_from_env",
]
