"""Enforcement shims for the tool-lane model deny list.

``server.py`` is a size-capped control-plane file, so each place that must
refuse a blocked model calls one function here.  The decision rule itself is
the pure ``sonder_runtime.domain.tool_lane_models``; this adapter only supplies
the deny list from the runtime policy and shapes the result for the caller.

Every lane check takes the loaded runtime policy the caller already holds.  A
missing policy is read from disk without creating it, and a policy that cannot
be read fails closed for caller-named models.  The generation checks
(``require_generation``, ``require_served``) read a deny list cached per
version of the policy file.

A tier bound to a non-Ollama provider (``SONDER_<TIER>_PROVIDER`` /
``SONDER_MODEL_BACKEND``) is judged by the model that provider serves, which
``served_model`` reads from the same environment the provider reads on every
call; the Ollama name in ``TIERS`` is not what answers such a tier.  A
bridged call is always a tier call (a model asked for by name stays on
Ollama, and the bridged request carries no ``model`` option), so a blocked
model never serves a tier: the bridge refuses it before sending when the
provider is configured to serve it and after the reply when the reply
reports it, on every lane, text-only or not.  ``text_only`` keeps its
meaning for a model asked for by name through Ollama and for a bridged tier
whose served model is undetermined.  Sonder Inference and OpenRouter name
the served model in every reply, so an undetermined configured model is not
refused up front for them; only the OpenAI-compatible transport, whose
reply is never read for a model name, is refused in tool lanes when nothing
names its model.

A selector on the default route (``""``, ``sonder``, ``local``) is judged by
every tier that route can answer on: its start and every configured local
tier its escalation can reach (``default_route_refusal``).
``prompt_refusal`` judges a model, an ensemble or a tier a prompt's own text
selects, for lanes whose prompts come from data.
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


_SONDER_INFERENCE = "sonder_inference"
_OPENROUTER = "openrouter"
_OPENAI_COMPATIBLE = "openai_compatible"
# Providers whose reply always names the model it served: the Sonder
# Inference gateway rejects a reply that does not, and OpenRouter reports
# the reply's model, else the configured one it sent.  For them the reply's
# model is judged, so an undetermined configured model is not refused first.
REPLY_NAMES_MODEL = frozenset({_SONDER_INFERENCE, _OPENROUTER})


def _served_setting(provider: str, tier: str) -> str:
    """The provider setting that chooses the model it serves for ``tier``."""
    if provider == _SONDER_INFERENCE:
        return "SONDER_INFERENCE_TIER_MODELS entry %s=<model>, else SONDER_INFERENCE_MODEL" % tier
    if provider == _OPENROUTER:
        return ("SONDER_OPENROUTER_TIER_MODELS entry %s=<model> or the policy's "
                "provider_models.openrouter, else SONDER_OPENROUTER_MODEL" % tier)
    if provider == _OPENAI_COMPATIBLE:
        return "SONDER_OPENAI_MODEL"
    return ""


def _served_message(tier: Any, provider: Any, verdict: _models.ModelLaneDecision) -> str:
    """``served_refusal_message`` naming the provider's own setting.

    The OpenAI-compatible transport sends ``SONDER_OPENAI_MODEL``, else the
    tier name, and reports what it sent, so with that variable unset nothing
    names the served model.
    """
    provider = str(provider or "")
    tier = str(tier or "")
    fix = ""
    if provider == _OPENAI_COMPATIBLE:
        fix = ("SONDER_OPENAI_MODEL is not set and that transport never reads a model "
               "name from the reply; set SONDER_OPENAI_MODEL to the model that endpoint serves")
    return _models.served_refusal_message(
        tier, provider, verdict, fix=fix, setting=_served_setting(provider, tier),
    )


def _configured_verdict(provider: str, model: Any, deny: _models.ToolLaneDenyList) -> _models.ModelLaneDecision:
    """The verdict for the model ``provider`` is configured to serve, judged before any reply.

    An undetermined model is not a refusal for a provider whose reply names
    the model (``REPLY_NAMES_MODEL``): that reply is judged instead.
    """
    verdict = _models.decide_served(model, deny)
    if verdict.status is _models.ModelLaneStatus.SERVED_UNKNOWN and provider in REPLY_NAMES_MODEL:
        return _models.ModelLaneDecision(_models.ModelLaneStatus.ALLOWED, verdict.model)
    return verdict


def _reported_verdict(reported: Any, deny: _models.ToolLaneDenyList) -> _models.ModelLaneDecision:
    """The verdict for the model a reply says it served.

    An empty or ``default`` id, or one that only echoes a tier or the default
    route back (what the OpenAI-compatible transport reports when no model is
    configured), names no model.
    """
    from sonder_runtime.adapters.provider_bindings import PROVIDER_TIERS

    raw = str(reported if reported is not None else "").strip()
    if raw.lower() in PROVIDER_TIERS or raw.lower() in _DEFAULT_ROUTE:
        return _models.ModelLaneDecision(_models.ModelLaneStatus.SERVED_UNKNOWN, raw)
    return _models.decide_served(raw, deny)


def _judge_reply(verdict: _models.ModelLaneDecision, text_only: bool) -> bool:
    """Whether a reply's verdict refuses it: a blocked model always, an unnamed one unless text-only."""
    if verdict.status is _models.ModelLaneStatus.SERVED_UNKNOWN:
        return not text_only
    return verdict.denied


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
    """Refuse one model call before sending.

    The default for every generation is that the reply may be run, applied or
    turned into a tool call, so a denied model is refused: the model the call
    names and the payload's own ``model`` (as tier-bound names, so an
    unreadable policy still refuses the built-in list without refusing an
    ordinary model).  A caller whose reply is only returned or shown passes
    ``text_only=True`` and those names are not judged.

    A bridged ``rung`` is a tier call, and a blocked model never serves a
    tier: the model its provider is configured to serve is judged whatever
    ``text_only`` says.  When that model cannot be determined here the check
    is left to the reply (``require_served``).  A text-only call with no
    bridged rung reads no policy.  Raises a ``ModelCallError`` (kind
    ``configuration``, status 403, ``attempts=0``, ``tool_lane_refused=True``)
    carrying the refusal text.
    """
    if text_only and rung is None:
        return
    deny = generation_deny_list(policy)
    if not text_only:
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
        raise _generation_refused(_served_message(tier, provider, verdict))


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
    is judged on every call.  A blocked model is refused whatever
    ``text_only`` says (a blocked model never serves a tier).  An empty or
    ``default`` id, or one that only echoes a tier or the default route back
    (what the OpenAI-compatible transport reports when no model is
    configured), names no model: refused unless the call is text-only.
    Raises the same 403 ``ModelCallError`` as ``require_generation``; the
    reply is discarded.
    """
    provider = str(getattr(rung, "provider", "") or "")
    tier = str(getattr(rung, "tier", "") or "").strip().lower()
    verdict = _reported_verdict(reported, generation_deny_list(policy))
    if _judge_reply(verdict, text_only):
        raise _generation_refused(_served_message(tier, provider, verdict))


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
    """Refusal for a tier whose non-Ollama provider serves a blocked model, or an
    undetermined one that its reply cannot name (``_configured_verdict``)."""
    if not tier or _free_form(tier):
        return ""
    label = str(tier).strip().lower()
    try:
        served = served_model(label)
    except Exception as exc:  # noqa: BLE001 - an unreadable binding fails closed
        _warn_once("provider configuration could not be read", exc)
        verdict = _models.ModelLaneDecision(_models.ModelLaneStatus.SERVED_UNKNOWN, "")
        return _served_message(label, "", verdict)
    if served is None:
        return ""
    provider, model = served
    verdict = _configured_verdict(provider, model, deny)
    return _served_message(label, provider, verdict) if verdict.denied else ""


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
    undeterminable served model refuses, except for a provider whose reply
    names the model (``REPLY_NAMES_MODEL``), whose reply ``reply_refusal``
    judges.  ``bindings`` is the gateway's own ``ProviderBindings``.
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
        return _served_message(selector, "", verdict)
    verdict = _configured_verdict(provider, model, deny)
    return _served_message(selector, provider, verdict) if verdict.denied else ""


def _tier_call(tier: Any, explicit_model: Any) -> str:
    """The routing key of a packaged request that asks for a tier, or ``""`` for one by name."""
    from sonder_runtime.adapters.provider_bindings import PROVIDER_TIERS

    selector = str(tier or "").strip().lower() or "sonder"
    explicit = explicit_model.strip() if isinstance(explicit_model, str) else ""
    if explicit or (selector not in PROVIDER_TIERS and selector != "sonder"):
        return ""
    return selector


def _dispatched_provider(selector: str, bindings: Any) -> str:
    """The provider a packaged gateway sends ``selector`` to (the tier's binding, else the default)."""
    return str(bindings.tier_providers.get(selector) or bindings.default_generation_provider or "")


def tier_serving_refusal(
    tier: Any, explicit_model: Any, bindings: Any, policy: Mapping[str, Any] | None = None,
) -> str:
    """Before sending a packaged text request: refused when a bridged tier's provider serves a blocked model.

    Only a tier call is judged (a request that names a model, by tier or by
    an explicit ``model`` option, is a by-name request and answers as text).
    An Ollama tier and an undetermined served model pass; the reply is judged
    by ``reply_refusal``.  A provider configuration that cannot be read
    passes too: the provider reads the same configuration and fails itself,
    and any reply it produces is still judged.
    """
    from sonder_runtime.application.chat.provider_bridge import LEGACY_PROVIDER

    selector = _tier_call(tier, explicit_model)
    if not selector:
        return ""
    try:
        provider = _dispatched_provider(selector, bindings)
        if provider in ("", LEGACY_PROVIDER):
            return ""
        model = _provider_model(provider, selector, "", None)
    except Exception as exc:  # noqa: BLE001 - the reply's served model is judged instead
        _warn_once("provider configuration could not be read before sending", exc)
        return ""
    verdict = _models.decide_served(model, generation_deny_list(policy))
    if verdict.status is _models.ModelLaneStatus.DENIED:
        return _served_message(selector, provider, verdict)
    return ""


def reply_refusal(
    tier: Any, explicit_model: Any, bindings: Any, reported: Any, *,
    text_only: bool, policy: Mapping[str, Any] | None = None,
) -> str:
    """After a packaged reply from a non-Ollama provider: refused when it reports a blocked or unnamed model.

    A blocked reported model refuses a tier call on every lane and any
    request on a tool lane; an unnamed one refuses unless ``text_only``.  A
    text request that names a model is not judged here, and neither is a
    request the gateway sends to Ollama (judged before sending).
    """
    from sonder_runtime.application.chat.provider_bridge import LEGACY_PROVIDER

    selector = _tier_call(tier, explicit_model)
    if text_only and not selector:
        return ""
    key = selector or str(tier or "").strip().lower() or "sonder"
    try:
        provider = _dispatched_provider(key, bindings)
    except Exception as exc:  # noqa: BLE001 - an unreadable binding fails closed
        _warn_once("provider configuration could not be read after the reply", exc)
        provider = ""
    if provider == LEGACY_PROVIDER:
        return ""
    verdict = _reported_verdict(reported, generation_deny_list(policy))
    return _served_message(key, provider, verdict) if _judge_reply(verdict, text_only) else ""


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


def _tier_refusal(tier: str, bindings: Mapping[str, str], deny: _models.ToolLaneDenyList) -> str:
    """Refusal for one tier: the model it is bound to, then the model a bridge serves for it."""
    verdict = _models.decide(bindings.get(tier, ""), deny, free_form=False)
    if verdict.denied:
        return _models.refusal_message(verdict.model, verdict.status)
    return _served_refusal(tier, deny)


def default_route_reach(
    bindings: Mapping[str, str], policy: Mapping[str, Any] | None = None,
) -> tuple[str, tuple[str, ...]]:
    """``(start, tiers)``: where the default route starts and every tier it can answer on.

    ``server._serve_target`` starts the default route on the policy's chat
    tier (on the ``sonder`` alias when ``SONDER_STRICT`` is on or that tier
    is unbound), and ``server._default_route_plan`` hands
    ``tier_escalation.plan`` the configured local tiers
    (``runtime_policy.configured_tiers``) with no image.  The tiers it can
    reach are asked of the plan's own filter (``tier_escalation.reachable_tiers``,
    the union over prompts): a tier weaker than the start is never a rung,
    ``vision`` never is without an image, and with ``SONDER_MODEL_ESCALATION``
    off the start is the only rung.  An unbound tier is dropped (never a rung,
    ``_local_tier_rung``).  A policy that cannot be read starts on the
    fallback chat tier over every local tier in ``bindings`` (fails closed).
    """
    from sonder_runtime.application.routing import tier_escalation

    try:
        loaded = policy or _policy.load(create=False)
        chat = _policy.route_tier("chat", loaded, fallback="general")
        available = _policy.configured_tiers(loaded, bindings)
    except Exception as exc:  # noqa: BLE001 - an unknown route fails closed
        _warn_once("default route tiers could not be read", exc)
        chat = "general"
        available = tuple(tier for tier in _policy.LOCAL_TIERS if tier in bindings)
    strict = str(os.environ.get("SONDER_STRICT", "")).strip().lower() in ("1", "true", "yes", "on")
    start = "sonder" if strict or not str(bindings.get(chat) or "").strip() else chat
    escalate = tier_escalation.enabled(os.environ.get(tier_escalation.KNOB))
    reach = tier_escalation.reachable_tiers(start, available, escalate=escalate)
    tiers = tuple(tier for tier in reach if str(bindings.get(tier) or "").strip())
    return start, tiers


def default_route_refusal(
    bindings: Mapping[str, str], policy: Mapping[str, Any] | None = None,
    deny: _models.ToolLaneDenyList | None = None,
) -> str:
    """Refusal for a lane that runs a reply from the default route, else ``""``.

    The default route (``""``, ``sonder``, ``local``) starts on the policy's
    chat tier and escalates, when an answer fails or is empty, over the
    configured local tiers the escalation plan can reach from there
    (``default_route_reach``).  The lane cannot know which rung will answer,
    so every one is judged: the model the tier is bound to and, for a bridged
    tier, the model its provider serves (``_served_refusal``: denied, or
    undetermined for a provider whose reply does not name it).  The start
    tier is refused with its own text; any other tier names the escalation.
    """
    deny = deny_list(policy) if deny is None else deny
    start, reachable = default_route_reach(bindings, policy)
    for tier in reachable:
        refused = _tier_refusal(tier, bindings, deny)
        if refused:
            return refused if tier == start else _models.default_route_refusal_message(tier, refused)
    return ""


def selector_refusal(
    tiers: Any, bindings: Mapping[str, str], policy: Mapping[str, Any] | None = None,
) -> str:
    """Refusal text when a comma-separated tier/model selector is blocked, else ``""``.

    For lanes that generate through a text entry (chat, ensemble) and then
    execute or apply the reply.  Nothing is resolved, so no catalog is read.
    A default-route selector (``""``, ``sonder``, ``local``) is judged by
    every tier the default route can answer on (``default_route_refusal``).
    """
    deny = deny_list(policy)
    for selector in str(tiers or "").split(","):
        if selector.strip().lower() in _DEFAULT_ROUTE:
            refused = default_route_refusal(bindings, policy, deny)
        else:
            verdict = _models.decide_selector(selector, bindings, deny)
            if verdict.denied:
                return _models.refusal_message(verdict.model, verdict.status)
            key = selector.strip().lower()
            refused = _served_refusal(key, deny) if key in bindings else ""
        if refused:
            return refused
    return ""


def prompt_refusal(
    prompt: Any, recognize: Callable[[Any], Any], bindings: Mapping[str, str],
    policy: Mapping[str, Any] | None = None,
) -> str:
    """Refusal when a prompt's own text picks a refused model, else ``""``.

    ``server.sonder`` re-routes a prompt that starts with an explicit model
    request (``use model X: ...``, ``ask code and reasoning models: ...``;
    ``recognize`` is ``server.natural_model_request``) to that model or
    ensemble, overriding the caller's tier and ``model_override``.  A lane
    that runs the reply and whose prompt text it does not write itself (a
    dataset or generated task) judges that target too.  A fanout request is
    not judged here: the fanout sweep leaves every denied model out
    (``sweep_skip_reason``).

    A prompt that is a console command is answered by that command
    (``server.control_command``).  ``/ensemble`` polls the bound local tiers,
    which can include a tier the default route never reaches, so each
    configured local tier is judged by name; ``/vision`` (``/analyzeimage``)
    answers on the vision tier, which is judged.  The other commands that
    generate (``/work``, ``/game``, ``/delegate``, ``/master``) reach lanes
    that refuse on their own.
    """
    request = recognize(prompt)
    if isinstance(request, Mapping):
        if request.get("kind") == "model":
            return selector_refusal(request.get("model"), bindings, policy)
        if request.get("kind") == "ensemble":
            return selector_refusal(request.get("tiers"), bindings, policy)
        return ""
    command = str(prompt or "").strip().split(None, 1)
    command = command[0].lower() if command else ""
    if command == "/ensemble":
        try:
            tiers = _policy.configured_tiers(policy or _policy.load(create=False), bindings)
        except Exception as exc:  # noqa: BLE001 - an unknown tier set fails closed
            _warn_once("ensemble tiers could not be read", exc)
            tiers = tuple(tier for tier in _policy.LOCAL_TIERS if tier in bindings)
        return selector_refusal(",".join(tiers), bindings, policy) if tiers else ""
    if command in ("/vision", "/analyzeimage"):
        return selector_refusal("vision", bindings, policy)
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
