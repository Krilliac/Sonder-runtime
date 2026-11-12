"""Enforcement shims for the tool-lane model deny list.

``server.py`` is a size-capped control-plane file, so each place that must
refuse a blocked model calls one function here.  The decision rule itself is
the pure ``sonder_runtime.domain.tool_lane_models``; this adapter only supplies
the deny list from the runtime policy and shapes the result for the caller.

Every function takes the loaded runtime policy the caller already holds.  A
missing policy is read from disk without creating it, and a policy that cannot
be read fails closed for caller-named models.

A tier bound to a non-Ollama provider (``SONDER_<TIER>_PROVIDER`` /
``SONDER_MODEL_BACKEND``) is judged by the model that provider serves, which
``served_model`` reads from the same environment the provider reads on every
call; the Ollama name in ``TIERS`` is not what answers such a tier.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping, MutableMapping
from typing import Any

import sonder_runtime.adapters.runtime_policy as _policy
from sonder_runtime.domain import tool_lane_models as _models

# The tier label ``_serve_target`` returns (with no model) for a caller-named
# model that may not be used outside chat.
DENIED_LABEL = "tool-lane-denied"
SWEEP_SKIP_REASON = "chat by name only (tool-lane deny list)"
_DEFAULT_ROUTE = ("", "sonder", "local")

_logger = logging.getLogger(__name__)
_warned: set[str] = set()


def _warn_once(what: str, exc: BaseException) -> None:
    """Log a swallowed guard failure once per kind at warning level."""
    if what not in _warned:
        _warned.add(what)
        _logger.warning("tool-lane guard: %s (%s); failing closed", what, type(exc).__name__)


def deny_list(policy: Mapping[str, Any] | None = None) -> _models.ToolLaneDenyList:
    """The deny list in force; unreadable when the policy cannot be loaded."""
    try:
        return _policy.tool_lane_deny_list(policy or _policy.load(create=False))
    except Exception as exc:  # noqa: BLE001 - an unreadable policy fails closed
        _warn_once("runtime policy could not be loaded", exc)
        return _policy.tool_lane_deny_list(None)


def decision(
    model: Any, policy: Mapping[str, Any] | None = None, *, free_form: bool,
) -> _models.ModelLaneDecision:
    return _models.decide(model, deny_list(policy), free_form=free_form)


def _free_form(tier_label: Any) -> bool:
    """A ``model:<name>`` label marks a model the caller named directly."""
    return str(tier_label or "").startswith("model:")


def served_model(tier: Any, env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """``(provider, model)`` a non-Ollama tier actually sends, or None for Ollama.

    Mirrors each provider's own choice for a bridged rung, which never carries
    a per-call ``model`` option: Sonder Inference sends its tier map entry,
    else ``SONDER_INFERENCE_MODEL``; OpenRouter its tier model (environment
    over policy), else its default; the OpenAI-compatible transport
    ``SONDER_OPENAI_MODEL``, else the tier name itself.  ``model`` is ``""``
    when nothing names it.  Raises when the configuration cannot be read.
    """
    from sonder_runtime.adapters.provider_bindings import (
        PROVIDER_TIERS,
        provider_bindings_from_env,
    )

    label = str(tier or "").strip().lower()
    if label not in PROVIDER_TIERS:
        return None  # model pins, the strict alias and cloud tiers stay on Ollama
    provider = provider_bindings_from_env(env).tier_providers.get(label, "ollama")
    if provider == "ollama":
        return None
    if provider == "sonder_inference":
        from sonder_runtime.adapters.inference.sonder_inference_gateway import (
            config_from_env,
        )

        settings = config_from_env(env)
        return provider, str(settings.tier_models.get(label, settings.model) or "").strip()
    if provider == "openrouter":
        from sonder_runtime.adapters.inference.openrouter_gateway import config_from_env

        return provider, str(config_from_env(env).model_for_tier(label) or "").strip()
    if provider == "openai_compatible":
        source = os.environ if env is None else env
        return provider, str(source.get("SONDER_OPENAI_MODEL", "") or "").strip()
    return provider, ""


def _served_refusal(tier: Any, deny: _models.ToolLaneDenyList) -> str:
    """Refusal for a tier whose non-Ollama provider serves a blocked or unknown model."""
    if not tier or _free_form(tier):
        return ""
    label = str(tier).strip().lower()
    try:
        served = served_model(label)
    except Exception as exc:  # noqa: BLE001 - an unreadable binding fails closed
        _warn_once("provider configuration could not be read", exc)
        verdict = _models.ModelLaneDecision(_models.ModelLaneStatus.SERVED_UNKNOWN, "")
        return _models.served_refusal_message(label, "", verdict)
    if served is None:
        return ""
    provider, model = served
    verdict = _models.decide_served(model, deny)
    return _models.served_refusal_message(label, provider, verdict) if verdict.denied else ""


def denied(model: Any, tier_label: Any = "", policy: Mapping[str, Any] | None = None) -> bool:
    """True when ``model`` may not drive tools or have its output executed.

    A non-Ollama tier label is also judged by the model its provider serves.
    """
    deny = deny_list(policy)
    if _models.decide(model, deny, free_form=_free_form(tier_label)).denied:
        return True
    return bool(_served_refusal(tier_label, deny))


def refusal(name: Any, policy: Mapping[str, Any] | None = None) -> str:
    """The refusal text for a caller-named model blocked from tool-using lanes."""
    return _models.refusal_message(name, decision(name, policy, free_form=True).status)


def resolved_refusal(
    selector: Any, model: Any, tier_label: Any, policy: Mapping[str, Any] | None = None,
) -> str:
    """Refusal text for a tool lane's resolved target, or ``""`` when it may run.

    Covers a selector ``_serve_target`` refused, a prepared target, a tier
    that somehow holds a blocked model, and a non-Ollama tier whose served
    model is blocked or cannot be determined.
    """
    if tier_label == DENIED_LABEL:
        return refusal(selector, policy)
    deny = deny_list(policy)
    verdict = _models.decide(model, deny, free_form=_free_form(tier_label))
    if verdict.denied:
        return _models.refusal_message(verdict.model, verdict.status)
    return _served_refusal(tier_label, deny)


def _selector_tier(selector: str, bindings: Mapping[str, str], policy: Mapping[str, Any] | None) -> str:
    """The tier a selector reaches without resolving it ("" for a model name).

    The default route reaches the policy's chat tier (``_serve_target`` only
    swaps in the Ollama alias when strict, so judging the chat tier is the
    conservative choice).
    """
    key = selector.strip().lower()
    if key in _DEFAULT_ROUTE:
        return _policy.route_tier("chat", policy or _policy.load(create=False), fallback="general")
    return key if key in bindings else ""


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
        try:
            tier = _selector_tier(selector, bindings, policy)
        except Exception as exc:  # noqa: BLE001 - an unknown route fails closed
            _warn_once("default route tier could not be read", exc)
            tier = "general"
        refused = _served_refusal(tier, deny)
        if refused:
            return refused
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
