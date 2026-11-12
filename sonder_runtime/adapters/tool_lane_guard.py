"""Enforcement shims for the tool-lane model deny list.

``server.py`` is a size-capped control-plane file, so each place that must
refuse a blocked model calls one function here.  The decision rule itself is
the pure ``sonder_runtime.domain.tool_lane_models``; this adapter only supplies
the deny list from the runtime policy and shapes the result for the caller.

Every function takes the loaded runtime policy the caller already holds.  A
missing policy is read from disk without creating it, and a policy that cannot
be read fails closed for caller-named models.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from typing import Any

import sonder_runtime.adapters.runtime_policy as _policy
from sonder_runtime.domain import tool_lane_models as _models

# The tier label ``_serve_target`` returns (with no model) for a caller-named
# model that may not be used outside chat.
DENIED_LABEL = "tool-lane-denied"
SWEEP_SKIP_REASON = "chat by name only (tool-lane deny list)"


def deny_list(policy: Mapping[str, Any] | None = None) -> _models.ToolLaneDenyList:
    """The deny list in force; unreadable when the policy cannot be loaded."""
    try:
        return _policy.tool_lane_deny_list(policy or _policy.load(create=False))
    except Exception:
        return _policy.tool_lane_deny_list(None)


def decision(
    model: Any, policy: Mapping[str, Any] | None = None, *, free_form: bool,
) -> _models.ModelLaneDecision:
    return _models.decide(model, deny_list(policy), free_form=free_form)


def _free_form(tier_label: Any) -> bool:
    """A ``model:<name>`` label marks a model the caller named directly."""
    return str(tier_label or "").startswith("model:")


def denied(model: Any, tier_label: Any = "", policy: Mapping[str, Any] | None = None) -> bool:
    """True when ``model`` may not drive tools or have its output executed."""
    return decision(model, policy, free_form=_free_form(tier_label)).denied


def refusal(name: Any, policy: Mapping[str, Any] | None = None) -> str:
    """The refusal text for a caller-named model blocked from tool-using lanes."""
    return _models.refusal_message(name, decision(name, policy, free_form=True).status)


def resolved_refusal(
    selector: Any, model: Any, tier_label: Any, policy: Mapping[str, Any] | None = None,
) -> str:
    """Refusal text for a tool lane's resolved target, or ``""`` when it may run.

    Covers a selector ``_serve_target`` refused, a prepared target, and a tier
    that somehow holds a blocked model.
    """
    if tier_label == DENIED_LABEL:
        return refusal(selector, policy)
    verdict = decision(model, policy, free_form=_free_form(tier_label))
    return _models.refusal_message(verdict.model, verdict.status) if verdict.denied else ""


def selector_refusal(
    tiers: Any, bindings: Mapping[str, str], policy: Mapping[str, Any] | None = None,
) -> str:
    """Refusal text when a comma-separated tier/model selector is blocked, else ``""``.

    For lanes that generate through a text entry (chat, ensemble) and then
    execute or apply the reply.  Nothing is resolved, so no catalog is read.
    """
    deny = deny_list(policy)
    for selector in str(tiers or "").split(","):
        verdict = _models.decide_selector(selector, bindings, deny)
        if verdict.denied:
            return _models.refusal_message(verdict.model, verdict.status)
    return ""


def seed_bindings(bindings: Mapping[str, str]) -> dict[str, str]:
    """The environment's tier map with any blocked model replaced by its default."""
    return _models.allowed_bindings(
        bindings, _models.ToolLaneDenyList.from_policy(None), _policy.DEFAULT_MODELS,
    )


def bind_local_tiers(tiers: MutableMapping[str, str], policy: Mapping[str, Any]) -> None:
    """Copy the policy's local model bindings into the live tier map.

    A blocked model is never bound: that tier keeps its built-in default.  An
    optional tier the operator left unset must not be offered at all: dropping
    it keeps ``_serve_target``, ``available_tiers()`` and ``/v1/models`` honest,
    and the capability router sees it as unavailable and degrades to a base
    tier.
    """
    deny = deny_list(policy)
    for tier in _policy.LOCAL_TIERS:
        model = str(policy["local_models"].get(tier) or "").strip()
        if _models.decide(model, deny, free_form=False).denied:
            model = _policy.DEFAULT_MODELS.get(tier, "")
        if model:
            tiers[tier] = model
        else:
            tiers.pop(tier, None)


def chat_target(serve_target: Callable[..., tuple], tier: Any, strict: Any) -> tuple:
    """Resolve a target for a text-only caller: by-name chat, ensemble, prewarm.

    Only a target refused as tool-lane-denied is resolved again with
    ``chat_only=True``, so every other selector keeps the two-argument call.
    A caller that executes or applies the reply must use ``selector_refusal``
    first.
    """
    target = serve_target(tier, strict)
    if target[3] == DENIED_LABEL:
        return serve_target(tier, strict, chat_only=True)
    return target


def sweep_skip_reason(name: Any, policy: Mapping[str, Any] | None = None) -> str:
    """Why an automatic model sweep leaves ``name`` out, or ``""``."""
    return SWEEP_SKIP_REASON if decision(name, policy, free_form=False).denied else ""
