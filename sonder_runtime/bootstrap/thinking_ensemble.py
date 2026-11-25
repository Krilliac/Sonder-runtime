"""Composition-root helpers for bound thinking-tier deliberation and auto-ensembling.

Coordinates:
1. Main-workstation Sonder delegating its pre-turn ``Thinking`` phase to a bound
   local thinking tier (``thinking`` or ``reasoning``) when distinct from the
   primary model, recording reasoning via ``activity_tracker`` and the active
   ``AgentProgressBoard`` while failing soft on timeout/unreachability.
2. Automatic ``code`` and ``security`` lane ensembling with the bound local
   thinking tier across ``ensemble_answer``, ``ensemble_codegen_build_loop``,
   ``offload``, and delegated ``coder``/``security`` child lanes.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.application.agents import delegation_runtime as _delegation
from sonder_runtime.application.chat import provider_bridge as _provider_bridge
from sonder_runtime.application.tasks.account_scope import active_task_account_scope
from sonder_runtime.domain import thinking_controls as _thinking_controls
from sonder_runtime.domain.routing import thinking_ensemble as _domain

_logger = logging.getLogger(__name__)
_IN_THINKING_DELEGATION: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "sonder_in_thinking_delegation", default=False,
)
_DEFAULT_THINKING_NUM_PREDICT = 512
_DEFAULT_THINKING_TIMEOUT = 45
# Note: urllib.error.URLError inherits from OSError, so OSError covers transport errors cleanly.
_SOFT_ERRORS = (OSError, RuntimeError, ValueError, TypeError, LookupError, ModelCallError)


def thinking_ensemble_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return True unless explicitly disabled in process environment."""
    return _domain.thinking_ensemble_enabled(os.environ if environ is None else environ)


@dataclass(frozen=True)
class ThinkingDelegationResult:
    """Captured thinking-tier deliberation for one turn or delegated lane."""

    tier: str
    model: str
    thinking: str
    brief: str
    elapsed_ms: int


def in_thinking_delegation() -> bool:
    """Return True inside an active thinking-tier delegation or auto-ensemble call."""
    return bool(_IN_THINKING_DELEGATION.get())


@contextlib.contextmanager
def thinking_delegation_scope() -> Iterator[None]:
    """Guard against recursive thinking-tier delegation or nested auto-ensembling."""
    token = _IN_THINKING_DELEGATION.set(True)
    try:
        yield
    finally:
        _IN_THINKING_DELEGATION.reset(token)


def _model_matches(target: str, candidate: str) -> bool:
    t = str(target or "").strip().casefold()
    c = str(candidate or "").strip().casefold()
    if not t or not c:
        return False
    if t == c:
        return True
    t_norm = t if ":" in t else f"{t}:latest"
    c_norm = c if ":" in c else f"{c}:latest"
    return t_norm == c_norm


def thinking_tier_reachable(server: Any, tier_name: str, model_name: str) -> bool:
    """Fast, read-only reachability check so offline workers degrade cleanly with zero delay."""
    if not model_name or _domain.is_cloud_model(model_name):
        return False
    bridge_fn = getattr(server, "_bridge_provider_for_tier", None)
    if callable(bridge_fn):
        with contextlib.suppress(*_SOFT_ERRORS):
            provider = bridge_fn(tier_name)
            if provider is not None:
                return not bool(_provider_bridge.is_hosted(provider))
    pool = getattr(server, "OLLAMA_POOL", None)
    if bool(getattr(pool, "enabled", False)):
        snaps_fn = getattr(pool, "snapshots", None)
        if callable(snaps_fn):
            with contextlib.suppress(*_SOFT_ERRORS):
                snaps = list(snaps_fn() or ())
                healthy = [s for s in snaps if getattr(s, "healthy", False)]
                if not healthy:
                    return False
                populated = [s for s in healthy if getattr(s, "models", None)]
                if not populated:
                    # Pool workers are healthy but model inventories have not been
                    # probed yet; allow the call and rely on fail-soft error handling.
                    return True
                for snap in populated:
                    models = getattr(snap, "models", ()) or ()
                    if any(_model_matches(model_name, str(m)) for m in models):
                        return True
                return False
    records_fn = getattr(server, "_runtime_installed_model_records", None)
    if callable(records_fn):
        with contextlib.suppress(*_SOFT_ERRORS):
            records = list(records_fn() or ())
            if not records:
                return False
            for entry in records:
                name = entry[0] if isinstance(entry, (tuple, list)) and entry else entry
                if _model_matches(model_name, str(name)):
                    return True
            return False
    return False


def _is_cloud_checker(server: Any) -> Callable[[str, str], bool]:
    raw_fn = getattr(server, "_is_cloud_tier", None)
    if callable(raw_fn):
        def _check(tier: str, model: str) -> bool:
            try:
                return bool(raw_fn(tier, model))
            except _SOFT_ERRORS:
                return _domain.is_cloud_model(model)
        return _check
    return lambda _tier, model: _domain.is_cloud_model(model)


def _record_thinking_visibility(
    server: Any,
    *,
    thinking_tier: str,
    thinking_model: str,
    thinking_text: str,
    brief_text: str,
    elapsed_ms: int,
    lane: str = "",
) -> None:
    """Publish thinking-tier deliberation to activity_tracker and active AgentProgressBoard."""
    tracker = getattr(server, "activity_tracker", None)
    exposure_fn = getattr(server, "reasoning_exposure_enabled", None)
    expose = bool(callable(exposure_fn) and exposure_fn())
    if tracker is not None:
        record_event = getattr(tracker, "record_event", None)
        if callable(record_event):
            with contextlib.suppress(*_SOFT_ERRORS):
                lane_label = f" ({lane})" if lane else ""
                record_event(
                    "thinking_tier",
                    summary=f"Thinking via {thinking_tier} ({thinking_model}){lane_label} in {elapsed_ms}ms",
                )
        if expose and thinking_text:
            record_reasoning = getattr(tracker, "record_reasoning", None)
            if callable(record_reasoning):
                with contextlib.suppress(*_SOFT_ERRORS):
                    record_reasoning(
                        f"[{thinking_tier}: {thinking_model}]\n{thinking_text}",
                        model=thinking_model,
                    )
    summary_src = thinking_text or brief_text
    if summary_src and _delegation.current_board() is not None:
        compact = " ".join(summary_src.split()).strip()[:260]
        with contextlib.suppress(*_SOFT_ERRORS):
            _delegation.emit_agent_event(
                phase="thinking",
                tier=thinking_tier,
                model=thinking_model,
                thinking=compact if expose else "",
                summary=f"[{thinking_tier}] {compact[:150]}",
                elapsed_ms=elapsed_ms,
            )


def deliberate_for_turn(
    server: Any,
    prompt: str,
    *,
    tier: str = "",
    model: str = "",
    cloud: bool = False,
    history: Sequence[Mapping[str, Any]] | None = None,
    lane: str = "",
    num_predict: int = _DEFAULT_THINKING_NUM_PREDICT,
    timeout: int = _DEFAULT_THINKING_TIMEOUT,
    check_reachable: Callable[[Any, str, str], bool] | None = None,
) -> ThinkingDelegationResult | None:
    """Consult the bound local thinking tier for a pre-turn reasoning brief (fail-soft)."""
    if (
        cloud
        or in_thinking_delegation()
        or not thinking_ensemble_enabled()
        or active_task_account_scope() is not None
    ):
        return None
    tiers = getattr(server, "TIERS", None)
    if not isinstance(tiers, Mapping):
        return None
    is_cloud_fn = _is_cloud_checker(server)
    effective_lane = (lane or tier or "").strip().lower()
    resolved = _domain.should_delegate_thinking(
        effective_lane,
        prompt,
        tiers,
        primary_model=model,
        enabled=True,
        cloud=cloud,
        is_cloud_fn=is_cloud_fn,
    )
    if resolved is None:
        return None
    thinking_tier, thinking_model = resolved
    reach_fn = check_reachable or thinking_tier_reachable
    if not reach_fn(server, thinking_tier, thinking_model):
        return None
    chat_request = getattr(server, "_chat_request", None)
    if not callable(chat_request):
        return None

    delib_prompt = _domain.build_thinking_prompt(
        prompt,
        lane=effective_lane,
        history_tail=history or (),
    )
    budget_predict = max(128, min(1024, int(num_predict or _DEFAULT_THINKING_NUM_PREDICT)))
    options: dict[str, Any] = {"temperature": 0.3, "num_predict": budget_predict}
    auto_ctx_fn = getattr(server, "_auto_model_context", None)
    if callable(auto_ctx_fn):
        with contextlib.suppress(*_SOFT_ERRORS):
            ctx_val = int(auto_ctx_fn(thinking_model) or 0)
            if ctx_val > 0:
                options["num_ctx"] = min(ctx_val, 16384)
    payload: dict[str, Any] = {
        "model": thinking_model,
        "messages": [{"role": "user", "content": delib_prompt}],
        "stream": False,
        "options": options,
    }
    pool = getattr(server, "OLLAMA_POOL", None)
    if _thinking_controls.pool_says_thinking(pool, thinking_model):
        budget_fn: Any = _thinking_controls.with_local_thinking_budget
        payload = budget_fn(
            payload,
            headroom=_thinking_controls.LOCAL_STEP_THINKING_HEADROOM,
        )
    keep_alive_fn = getattr(server, "_keep_alive_for", None)
    if callable(keep_alive_fn):
        with contextlib.suppress(*_SOFT_ERRORS):
            ka = keep_alive_fn(thinking_model)
            if ka is not None:
                payload["keep_alive"] = ka

    started = time.monotonic()
    try:
        with thinking_delegation_scope():
            bridge_fn = getattr(server, "_bridge_provider_for_tier", None)
            provider = bridge_fn(thinking_tier) if callable(bridge_fn) else None
            ctx_mgr: Any = (
                _provider_bridge.bind_rung(provider, thinking_tier)
                if provider is not None
                else contextlib.nullcontext()
            )
            with ctx_mgr:
                _out, raw_msg = chat_request(
                    payload,
                    model=thinking_model,
                    cloud=False,
                    timeout=max(5, int(timeout or _DEFAULT_THINKING_TIMEOUT)),
                    return_message=True,
                    text_only=True,
                )
    except _SOFT_ERRORS as exc:
        _logger.debug(
            "thinking-tier deliberation skipped (%s / %s): %s",
            thinking_tier,
            thinking_model,
            exc,
        )
        return None

    elapsed_ms = int((time.monotonic() - started) * 1000)
    thinking_text, brief_text = _domain.extract_thinking_and_brief(
        raw_msg if isinstance(raw_msg, (Mapping, str)) else str(raw_msg or "")
    )
    if not brief_text and not thinking_text:
        return None
    _record_thinking_visibility(
        server,
        thinking_tier=thinking_tier,
        thinking_model=thinking_model,
        thinking_text=thinking_text,
        brief_text=brief_text,
        elapsed_ms=elapsed_ms,
        lane=effective_lane,
    )
    return ThinkingDelegationResult(
        tier=thinking_tier,
        model=thinking_model,
        thinking=thinking_text,
        brief=brief_text,
        elapsed_ms=elapsed_ms,
    )


def augment_system_with_thinking(
    server: Any,
    effective_system: str,
    prompt: str,
    *,
    tier: str = "",
    model: str = "",
    cloud: bool = False,
    history: Sequence[Mapping[str, Any]] | None = None,
    lane: str = "",
    check_reachable: Callable[[Any, str, str], bool] | None = None,
) -> tuple[str, ThinkingDelegationResult | None]:
    """Attach an advisory thinking brief from the bound thinking tier before the tool manifest."""
    result = deliberate_for_turn(
        server,
        prompt,
        tier=tier,
        model=model,
        cloud=cloud,
        history=history,
        lane=lane,
        check_reachable=check_reachable,
    )
    if result is None:
        return effective_system, None
    block = _domain.format_thinking_brief(
        result.brief,
        thinking_tier=result.tier,
        thinking_model=result.model,
        lane=(lane or tier or "").strip().lower(),
    )
    if not block:
        return effective_system, result
    combined = f"{effective_system}\n\n{block}" if effective_system else block
    return combined, result


def maybe_auto_ensemble_offload(
    server: Any,
    prompt: str,
    *,
    tier: str = "",
    system: str = "",
    num_predict: int = 1024,
    num_ctx: int = 0,
    schema: Any = None,
    cloud: bool = False,
    fleet_synthesis: bool = False,
    check_reachable: Callable[[Any, str, str], bool] | None = None,
) -> str | None:
    """Auto-ensemble non-learning ``code`` / ``security`` offloads with the bound thinking tier."""
    lane = str(tier or "").strip().lower()
    if (
        cloud
        or schema is not None
        or fleet_synthesis
        or lane not in _domain.AUTO_ENSEMBLE_LANES
        or in_thinking_delegation()
        or not thinking_ensemble_enabled()
        or active_task_account_scope() is not None
    ):
        return None
    tiers = getattr(server, "TIERS", None)
    if not isinstance(tiers, Mapping):
        return None
    is_cloud_fn = _is_cloud_checker(server)
    resolved = _domain.resolve_thinking_tier(
        tiers,
        primary_tier=lane,
        primary_model=str(tiers.get(lane) or ""),
        is_cloud_fn=is_cloud_fn,
    )
    if resolved is None:
        return None
    thinking_tier, thinking_model = resolved
    reach_fn = check_reachable or thinking_tier_reachable
    if not reach_fn(server, thinking_tier, thinking_model):
        return None
    ensemble_fn = getattr(server, "ensemble_answer", None)
    if not callable(ensemble_fn):
        return None
    combined_prompt = f"{system.strip()}\n\n{prompt}" if str(system or "").strip() else prompt
    try:
        with thinking_delegation_scope():
            answer = str(
                ensemble_fn(
                    combined_prompt,
                    tiers=f"{lane},{thinking_tier}",
                    mode="code" if lane == "code" else "prose",
                    num_predict=max(256, int(num_predict or 1024)),
                    num_ctx=int(num_ctx or 0),
                )
                or ""
            ).strip()
    except _SOFT_ERRORS as exc:
        _logger.debug("auto-ensemble offload skipped (%s + %s): %s", lane, thinking_tier, exc)
        return None
    if not answer or answer.startswith("ERROR:"):
        return None
    return answer


def resolve_ensemble_requested_tiers(
    tiers_arg: str,
    tiers_map: Mapping[str, str],
    configured_local_tiers: Sequence[str],
    skip_tiers: frozenset[str] | set[str] | Sequence[str],
    *,
    auto_expand: bool = True,
    is_cloud_fn: Callable[[str, str], bool] | None = None,
) -> tuple[list[str], bool]:
    """Return ``(requested_tiers, explicit)`` with automatic code/security thinking-tier pairing."""
    raw = [t.strip().lower() for t in (tiers_arg or "").split(",") if t.strip()]
    explicit = bool(raw)
    if not raw:
        skip = set(skip_tiers)
        return [t for t in configured_local_tiers if t not in skip], False
    allow_expand = bool(
        auto_expand
        and not in_thinking_delegation()
        and thinking_ensemble_enabled()
    )
    expanded = _domain.expand_lane_ensemble_tiers(
        raw,
        tiers_map,
        enabled=allow_expand,
        is_cloud_fn=is_cloud_fn,
    )
    return expanded, explicit


def resolve_codegen_ensemble_tiers(
    tiers_map: Mapping[str, str],
    *,
    is_cloud_fn: Callable[[str, str], bool] | None = None,
) -> str:
    """Return ``code,<thinking_tier>`` for ``ensemble_codegen_build_loop``."""
    resolved = _domain.resolve_thinking_tier(
        tiers_map,
        primary_tier="code",
        primary_model=str(tiers_map.get("code") or ""),
        is_cloud_fn=is_cloud_fn,
    )
    return f"code,{resolved[0]}" if resolved is not None else "code,reasoning"


def normalize_tier_alias(tier: str, tiers_map: Mapping[str, str]) -> str:
    """Resolve ``thinking`` alias to ``reasoning`` when not separately bound."""
    return _domain.normalize_tier_alias(tier, tiers_map)


def status_dict(
    tiers_map: Mapping[str, str],
    *,
    is_cloud_fn: Callable[[str, str], bool] | None = None,
) -> dict[str, Any]:
    """Return a JSON-safe status snapshot for ``runtime_policy_data``."""
    return _domain.thinking_ensemble_status(
        tiers_map,
        enabled=thinking_ensemble_enabled(),
        is_cloud_fn=is_cloud_fn,
    )


__all__ = [
    "ThinkingDelegationResult",
    "augment_system_with_thinking",
    "deliberate_for_turn",
    "in_thinking_delegation",
    "maybe_auto_ensemble_offload",
    "normalize_tier_alias",
    "resolve_codegen_ensemble_tiers",
    "resolve_ensemble_requested_tiers",
    "status_dict",
    "thinking_delegation_scope",
    "thinking_tier_reachable",
]
