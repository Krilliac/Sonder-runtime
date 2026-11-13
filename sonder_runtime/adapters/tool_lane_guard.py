"""Enforcement shims for the tool-lane model deny list.

``server.py`` is a size-capped control-plane file, so each place that must
refuse a blocked model calls one function here.  The decision rule itself is
the pure ``sonder_runtime.domain.tool_lane_models``; this adapter only supplies
the deny list from the runtime policy and shapes the result for the caller.

Every lane check takes the loaded runtime policy the caller already holds.  A
missing policy is read from disk without creating it, and a policy that cannot
be read fails closed for caller-named models.  The generation checks
(``require_generation``, ``require_served``) read a deny list cached per
version of the policy file, and refuse unless the call site is text-only.

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


# The deny list the generation check last read, with the policy file identity
# it was read for: ``(key, deny list)``.  Only a readable policy is cached.
_GENERATION_CACHE: tuple[tuple, _models.ToolLaneDenyList] | None = None


def _policy_file_key() -> tuple | None:
    """Identity of the policy file (path, mtime, size, inode); None when it cannot be stat'ed."""
    path = _policy.policy_path()
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return (str(path), None)
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)


def generation_deny_list(policy: Mapping[str, Any] | None = None) -> _models.ToolLaneDenyList:
    """The deny list for the generation check, read from disk at most once per file version.

    Every model call asks, so the list is cached against the policy file's
    path, modification time, size and inode; an edit, a replacement or a
    removal reads it again.  A failed read is never cached: until the file is
    readable each call reads it again (and fails closed).  Thread-safe: the
    cache is one tuple replaced atomically, and a racing reader at worst
    reads the file itself.
    """
    global _GENERATION_CACHE
    if policy:
        return deny_list(policy)
    key = _policy_file_key()
    cached = _GENERATION_CACHE
    if key is not None and cached is not None and cached[0] == key:
        return cached[1]
    deny = deny_list(None)
    if key is not None and deny.readable and _policy_file_key() == key:
        _GENERATION_CACHE = (key, deny)
    return deny


def _generation_refused(message: str) -> Exception:
    """The error a refused model call raises: a 403 configuration failure, no attempt made."""
    from sonder_runtime.adapters.model_transport import ModelCallError

    error = ModelCallError("configuration", message, status=403, attempts=0)
    error.tool_lane_refused = True
    return error


def require_generation(
    model: Any,
    *,
    rung: Any = None,
    text_only: bool = False,
    payload: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
) -> None:
    """Refuse one model call unless its caller declared it text-only.

    The default for every generation is that the reply may be run, applied or
    turned into a tool call, so a denied model is refused; a caller whose
    reply is only returned or shown passes ``text_only=True`` and is never
    judged (no policy read either).  Judged before sending: the model the
    call names and the payload's own ``model`` (as tier-bound names, so an
    unreadable policy still refuses the built-in list without refusing an
    ordinary model), and for a bridged ``rung`` the model its provider is
    configured to serve.  When that served model cannot be determined here
    the check is left to the reply (``require_served``).  Raises a
    ``ModelCallError`` (kind ``configuration``, status 403, ``attempts=0``,
    ``tool_lane_refused=True``) carrying the refusal text.
    """
    if text_only:
        return
    deny = generation_deny_list(policy)
    names = [model]
    if isinstance(payload, Mapping) and payload.get("model") != model:
        names.append(payload.get("model"))
    for name in names:
        verdict = _models.decide(name, deny, free_form=False)
        if verdict.denied:
            raise _generation_refused(_models.refusal_message(verdict.model, verdict.status))
    if rung is None:
        return
    provider = str(getattr(rung, "provider", "") or "")
    tier = str(getattr(rung, "tier", "") or "").strip().lower()
    try:
        served = _provider_model(provider, tier, "", None)
    except Exception as exc:  # noqa: BLE001 - the reply's served model is judged instead
        _warn_once("provider configuration could not be read before sending", exc)
        return
    verdict = _models.decide_served(served, deny)
    if verdict.status is _models.ModelLaneStatus.DENIED:
        raise _generation_refused(_models.served_refusal_message(tier, provider, verdict))


def require_served(
    rung: Any,
    reported: Any,
    *,
    text_only: bool = False,
    policy: Mapping[str, Any] | None = None,
) -> None:
    """Refuse a bridged reply whose provider reports a denied or unnamed model.

    The other half of ``require_generation`` for a bridged rung: after the
    provider answers, the model it says it served (``ModelResponse.model``)
    is judged unless the caller declared the call text-only.  An empty or
    ``default`` id, or one that only echoes a tier or the default route back
    (what the OpenAI-compatible transport reports when no model is
    configured), names no model and is refused.  Raises the same 403
    ``ModelCallError`` as ``require_generation``; the reply is discarded.
    """
    if text_only:
        return
    from sonder_runtime.adapters.provider_bindings import PROVIDER_TIERS

    provider = str(getattr(rung, "provider", "") or "")
    tier = str(getattr(rung, "tier", "") or "").strip().lower()
    raw = str(reported if reported is not None else "").strip()
    if raw.lower() in PROVIDER_TIERS or raw.lower() in _DEFAULT_ROUTE:
        verdict = _models.ModelLaneDecision(_models.ModelLaneStatus.SERVED_UNKNOWN, raw)
    else:
        verdict = _models.decide_served(raw, generation_deny_list(policy))
    if verdict.denied:
        raise _generation_refused(_models.served_refusal_message(tier, provider, verdict))


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
    from sonder_runtime.application.chat.provider_bridge import LEGACY_PROVIDER

    label = str(tier or "").strip().lower()
    if label not in PROVIDER_TIERS:
        return None  # model pins, the strict alias and cloud tiers stay on Ollama
    provider = provider_bindings_from_env(env).tier_providers.get(label, LEGACY_PROVIDER)
    if provider == LEGACY_PROVIDER:
        return None
    return provider, _provider_model(provider, label, "", env)


def _provider_model(provider: str, key: str, explicit: str, env: Mapping[str, str] | None) -> str:
    """The model id ``provider``'s own gateway sends for routing key ``key``.

    An explicit per-call ``model`` option wins on every non-Ollama gateway.
    The OpenAI-compatible transport sends its configured model, else the key
    itself, which names a model only when it is not a tier or the alias.
    """
    from sonder_runtime.adapters.inference import (
        openrouter_gateway,
        sonder_inference_gateway,
    )
    from sonder_runtime.adapters.provider_bindings import (
        PROVIDER_LABEL_IDS,
        PROVIDER_TIERS,
    )

    if explicit:
        return explicit
    if provider == sonder_inference_gateway.PROVIDER_ID:
        settings = sonder_inference_gateway.config_from_env(env)
        return str(settings.tier_models.get(key, settings.model) or "").strip()
    if provider == openrouter_gateway.PROVIDER_ID:
        return str(openrouter_gateway.config_from_env(env).model_for_tier(key) or "").strip()
    if provider == PROVIDER_LABEL_IDS["openai-compatible"]:
        source = os.environ if env is None else env
        configured = str(source.get("SONDER_OPENAI_MODEL", "") or "").strip()
        named = key not in PROVIDER_TIERS and key.lower() not in _DEFAULT_ROUTE
        return configured or (key if named else "")
    return ""


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


def gateway_refusal(
    tier: Any,
    explicit_model: Any,
    resolve_target: Callable[..., Any],
    bindings: Any,
    policy: Mapping[str, Any] | None = None,
) -> str:
    """Refusal for one packaged ``ModelGateway`` request from a tool or apply lane.

    Three identities are judged, because each can be what answers: a model
    the caller named (the request tier when it is not a tier, and an
    explicit ``model`` option); the Ollama target the runtime resolves for
    the request; and, when the gateway routes the request to another
    provider (the tier's binding, else the default provider, which is what a
    uniform gateway uses for any name), the model that provider sends.  An
    undeterminable served model refuses.  ``bindings`` is the gateway's own
    ``ProviderBindings``.
    """
    from sonder_runtime.application.chat.provider_bridge import LEGACY_PROVIDER

    selector = str(tier or "") or "sonder"
    explicit = explicit_model.strip() if isinstance(explicit_model, str) else ""
    deny = deny_list(policy)
    target = resolve_target(selector, None)
    label = getattr(target, "tier_label", None)
    named = [explicit] if explicit else []
    if not label or _free_form(label):
        named.append(selector)
    for name in named:
        verdict = _models.decide(name, deny, free_form=True)
        if verdict.denied:
            return _models.refusal_message(verdict.model, verdict.status)
    refused = resolved_refusal(selector, getattr(target, "model", None), label, policy)
    if refused:
        return refused
    try:
        provider = bindings.tier_providers.get(selector) or bindings.default_generation_provider
        if provider == LEGACY_PROVIDER:
            return ""
        model = _provider_model(provider, selector, explicit, None)
    except Exception as exc:  # noqa: BLE001 - an unreadable provider configuration fails closed
        _warn_once("provider configuration could not be read", exc)
        verdict = _models.ModelLaneDecision(_models.ModelLaneStatus.SERVED_UNKNOWN, "")
        return _models.served_refusal_message(selector, "", verdict)
    verdict = _models.decide_served(model, deny)
    return _models.served_refusal_message(selector, provider, verdict) if verdict.denied else ""


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
