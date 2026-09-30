"""Pure runtime-policy rules (SPEC-3 Phase 2 extraction).

Moved verbatim from the root ``runtime_policy.py`` module, minus every
I/O concern. These functions read no environment and touch no files —
callers pass the environment mapping explicitly. The root module now
delegates here; behavior is unchanged.

The policy intentionally cannot configure cloud models for the local tiers,
permissions, filesystem roots, credentials, or cloud consent.  Its one
cloud-adjacent section, ``provider_models``, only names which model an
already-bound, already-consented hosted provider (OpenRouter) serves per tier:
it can never bind a tier to that provider, supply its key, or enable cloud.
"""
from __future__ import annotations

import re

from ..openrouter_policy import OpenRouterPolicyError, validate_model_id

VERSION = 1
# Tiers that are always bound to a model. These are the router's fallback
# floor (``capability_router._FALLBACK``) and the only tiers an execution lane
# may be pinned to, so they can never be left unset.
BASE_LOCAL_TIERS = ("fast", "code", "general")
# Specialist tiers the capability router prefers for reasoning/vision work.
# They are bound by default but may be explicitly unset (empty model), in which
# case the router degrades to a base tier exactly as it did before they existed.
OPTIONAL_LOCAL_TIERS = ("reasoning", "vision")
LOCAL_TIERS = BASE_LOCAL_TIERS + OPTIONAL_LOCAL_TIERS
ROUTING_LANES = ("chat", "router", "workbench", "autopilot", "fleet", "review")
# Environment values that explicitly leave an optional tier unbound.
UNSET_TOKENS = frozenset({"none", "off", "disabled", "-"})
DEFAULT_MODELS = {
    # setup_alias/bootstrap_engine owns hardware-aware base-model selection.
    # Runtime policy targets its stable alias rather than assuming a model
    # family or that several models fit concurrently on a new host.
    "fast": "sonder:latest",
    "code": "sonder:latest",
    "general": "sonder:latest",
    "reasoning": "",
    "vision": "",
}
# Embeddings are intentionally not a routing tier: they produce a separate
# vector space for memory/indexing and must never be picked for chat work.
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"
RESERVED_PERSONAL_MODEL = "sonder-personal:latest"
DEFAULT_ROUTING = {
    # Chat is a routing lane, not a model tier. Ordinary conversation uses
    # the general base tier unless the operator pins a request explicitly.
    "chat": "general",
    "router": "fast",
    "workbench": "code",
    "autopilot": "code",
    "fleet": "code",
    "review": "code",
}
# The NPU utility accelerator sits below the local tiers and never becomes
# a generative tier. Policy only selects a behavior mode per capability; it
# can never name models, paths, providers, or anything cloud-related.
NPU_MODES = ("off", "shadow", "prefer")
NPU_CAPABILITIES = ("routing", "embeddings")
DEFAULT_NPU = {"mode": "off", "routing": "", "embeddings": ""}
# Opt-in long-context overflow: a turn whose estimated prompt exceeds the
# threshold moves from its local tier to a model that keeps its speed at long
# context (for example a mixture-of-experts on the Ollama pool).  Off by
# default.  An empty model means "the bound reasoning tier's model", so the
# default assumes no model family.  Only the Ollama pool provider is accepted
# and, like every other policy model, never a cloud model.
OVERFLOW_PROVIDERS = ("ollama",)
OVERFLOW_MIN_THRESHOLD = 4096
OVERFLOW_MAX_THRESHOLD = 1_048_576
DEFAULT_LONG_CONTEXT_OVERFLOW = {
    "enabled": False,
    "threshold_tokens": 32768,
    "model": "",
    "provider": "ollama",
}
OVERFLOW_ENV = {
    "enabled": "SONDER_LONG_CONTEXT_OVERFLOW",
    "threshold_tokens": "SONDER_LONG_CONTEXT_THRESHOLD",
    "model": "SONDER_LONG_CONTEXT_MODEL",
}
# Hosted providers whose per-tier model choice the policy may carry.  The
# binding itself (SONDER_<TIER>_PROVIDER), the API key and cloud consent stay
# in host configuration.
PROVIDER_MODEL_PROVIDERS = ("openrouter",)
_TRUE_TOKENS = frozenset({"1", "true", "yes", "on", "enabled"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off", "disabled"})
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")


def is_cloud_name(value: str) -> bool:
    lowered = str(value or "").strip().lower()
    return "-cloud" in lowered or lowered.endswith(":cloud")


def is_reserved_personal_alias(value) -> bool:
    model = str(value or "").strip().casefold()
    for prefix in ("registry.ollama.ai/library/", "library/"):
        if model.startswith(prefix):
            model = model[len(prefix):]
            break
    if ":" not in model:
        model += ":latest"
    return model == RESERVED_PERSONAL_MODEL.casefold()


def validate_model(value, fallback: str, *, allow_unset: bool = False) -> str:
    """Validate one tier's model name.

    ``allow_unset`` (optional tiers only) lets an explicit empty value mean
    "this tier is not bound". A missing value falls back to that tier's
    built-in default; hardware-agnostic specialist defaults are themselves
    empty, so an older policy file gains the tier without assuming a model.
    """
    if allow_unset and value is not None and not str(value).strip():
        return ""
    if allow_unset and value is None and not str(fallback or "").strip():
        return ""
    model = str(value or fallback).strip()
    if not _MODEL_RE.fullmatch(model):
        raise ValueError("invalid local model name %r" % model)
    if is_cloud_name(model):
        raise ValueError("runtime policy local tiers cannot reference cloud models")
    if is_reserved_personal_alias(model):
        return RESERVED_PERSONAL_MODEL
    return model


def seed_model(env, tier: str) -> str:
    """Seed one tier's model from an explicit environment mapping.

    ``SONDER_REASONING=none`` / ``SONDER_VISION=off`` seed an optional tier as
    unbound, which is how a deployment without a reasoning or vision model
    expresses that; base tiers ignore the token and keep their default.
    """
    configured = str(env.get("SONDER_%s" % tier.upper(), "") or "").strip()
    if tier == "code" and is_cloud_name(configured):
        configured = str(env.get("SONDER_CODE_LOCAL", "") or "").strip()
    if tier in OPTIONAL_LOCAL_TIERS and configured.lower() in UNSET_TOKENS:
        return ""
    if is_reserved_personal_alias(configured):
        configured = ""
    if configured and not is_cloud_name(configured):
        return validate_model(configured, DEFAULT_MODELS[tier])
    return DEFAULT_MODELS[tier]


def seed_embedding_model(env) -> str:
    """Seed the independent local embedding binding without accepting cloud."""
    configured = str(env.get("SONDER_EMBED_MODEL", "") or "").strip()
    if configured and not is_cloud_name(configured):
        return validate_model(configured, DEFAULT_EMBEDDING_MODEL)
    return DEFAULT_EMBEDDING_MODEL


def default_policy(env) -> dict:
    """Built-in policy seeded from an explicit environment mapping."""
    return {
        "version": VERSION,
        "revision": 0,
        "local_models": {tier: seed_model(env, tier) for tier in LOCAL_TIERS},
        "embedding_model": seed_embedding_model(env),
        "routing": dict(DEFAULT_ROUTING),
        "npu": dict(DEFAULT_NPU),
        "long_context_overflow": dict(DEFAULT_LONG_CONTEXT_OVERFLOW),
        "provider_models": {},
        "updated_ts": 0,
        "source": "environment seed",
    }


def normalize_npu(raw, base) -> dict:
    if raw in (None, ""):
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("runtime policy npu must be an object")
    defaults = base if isinstance(base, dict) else dict(DEFAULT_NPU)
    mode = str(
        raw.get("mode") if raw.get("mode") is not None
        else defaults.get("mode") or "off"
    ).strip().lower()
    if mode not in NPU_MODES:
        raise ValueError(
            "runtime policy npu mode must be one of: %s" % ", ".join(NPU_MODES)
        )
    npu = {"mode": mode}
    for capability in NPU_CAPABILITIES:
        value = str(
            raw.get(capability) if raw.get(capability) is not None
            else defaults.get(capability) or ""
        ).strip().lower()
        if value and value not in NPU_MODES:
            raise ValueError(
                "runtime policy npu %s override must be one of: %s"
                % (capability, ", ".join(NPU_MODES))
            )
        npu[capability] = value
    return npu


def _overflow_bool(value, where: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in _TRUE_TOKENS:
        return True
    if text in _FALSE_TOKENS:
        return False
    raise ValueError("%s must be on or off" % where)


def _overflow_threshold(value, where: str) -> int:
    if isinstance(value, bool):
        raise ValueError("%s must be an integer" % where)
    try:
        threshold = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be an integer" % where) from exc
    if not OVERFLOW_MIN_THRESHOLD <= threshold <= OVERFLOW_MAX_THRESHOLD:
        raise ValueError(
            "%s must be between %d and %d tokens"
            % (where, OVERFLOW_MIN_THRESHOLD, OVERFLOW_MAX_THRESHOLD)
        )
    return threshold


def _overflow_model(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if is_cloud_name(text):
        raise ValueError("long-context overflow cannot use a cloud model")
    return validate_model(text, "")


def normalize_long_context_overflow(raw, base=None) -> dict:
    """Validate the ``long_context_overflow`` policy section."""
    if raw in (None, ""):
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("runtime policy long_context_overflow must be an object")
    unknown = set(raw) - set(DEFAULT_LONG_CONTEXT_OVERFLOW)
    if unknown:
        raise ValueError(
            "unknown long_context_overflow key(s): %s" % ", ".join(sorted(unknown))
        )
    defaults = base if isinstance(base, dict) else DEFAULT_LONG_CONTEXT_OVERFLOW
    merged = {**DEFAULT_LONG_CONTEXT_OVERFLOW, **defaults, **raw}
    provider = str(merged.get("provider") or "ollama").strip().lower()
    if provider not in OVERFLOW_PROVIDERS:
        raise ValueError(
            "long-context overflow provider must be one of: %s"
            % ", ".join(OVERFLOW_PROVIDERS)
        )
    return {
        "enabled": _overflow_bool(merged.get("enabled"), "long-context overflow enabled"),
        "threshold_tokens": _overflow_threshold(
            merged.get("threshold_tokens"), "long-context overflow threshold",
        ),
        "model": _overflow_model(merged.get("model")),
        "provider": provider,
    }


def effective_long_context_overflow(policy, env) -> dict:
    """The overflow settings in force: the policy section, then env overrides.

    ``SONDER_LONG_CONTEXT_OVERFLOW``, ``SONDER_LONG_CONTEXT_THRESHOLD`` and
    ``SONDER_LONG_CONTEXT_MODEL`` override the policy while they are set, so
    an operator can force the feature per process.  An invalid override is
    ignored and reported in ``error``; it never enables anything.  The
    result lists which keys the environment supplied in ``overrides``.
    """
    section = policy.get("long_context_overflow") if isinstance(policy, dict) else None
    try:
        settings = normalize_long_context_overflow(section)
    except ValueError:
        settings = dict(DEFAULT_LONG_CONTEXT_OVERFLOW)
    overrides = []
    errors = []
    env = env or {}
    parsers = {
        "enabled": lambda value: _overflow_bool(value, OVERFLOW_ENV["enabled"]),
        "threshold_tokens": lambda value: _overflow_threshold(
            value, OVERFLOW_ENV["threshold_tokens"],
        ),
        "model": _overflow_model,
    }
    for key, variable in OVERFLOW_ENV.items():
        raw = str(env.get(variable, "") or "").strip()
        if not raw:
            continue
        try:
            settings[key] = parsers[key](raw)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        overrides.append(key)
    return {**settings, "overrides": tuple(overrides), "error": "; ".join(errors)}


def normalize_provider_models(raw) -> dict:
    """Validate ``provider_models``: ``{provider: {tier: model id}}``.

    An empty model removes that tier's mapping; an empty provider map is
    dropped, so the section stays absent until an operator uses it.
    """
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise ValueError("runtime policy provider_models must be an object")
    result: dict[str, dict[str, str]] = {}
    for provider, tiers in raw.items():
        name = str(provider or "").strip().lower()
        if name not in PROVIDER_MODEL_PROVIDERS:
            raise ValueError(
                "runtime policy provider_models supports only: %s"
                % ", ".join(PROVIDER_MODEL_PROVIDERS)
            )
        if tiers in (None, ""):
            continue
        if not isinstance(tiers, dict):
            raise ValueError("runtime policy provider_models.%s must be an object" % name)
        mapping: dict[str, str] = {}
        for tier, model in tiers.items():
            tier_name = str(tier or "").strip().lower()
            if tier_name not in LOCAL_TIERS:
                raise ValueError(
                    "runtime policy provider_models.%s names unknown tier %r (tiers: %s)"
                    % (name, tier, ", ".join(LOCAL_TIERS))
                )
            if model is None or not str(model).strip():
                continue
            try:
                mapping[tier_name] = validate_model_id(
                    str(model), "provider_models.%s.%s" % (name, tier_name),
                )
            except OpenRouterPolicyError as exc:
                raise ValueError(str(exc)) from exc
        if mapping:
            result[name] = {tier: mapping[tier] for tier in LOCAL_TIERS if tier in mapping}
    return result


def normalize(payload, defaults=None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("runtime policy must be a JSON object")
    # Environment variables seed a file only when it is first created. Once
    # a shared policy exists, normalization and recovery use stable
    # built-ins so separately launched surfaces cannot drift with their
    # inherited env.
    base = default_policy(env={}) if defaults is None else defaults
    raw_models = payload.get("local_models") or {}
    raw_routing = payload.get("routing") or {}
    if not isinstance(raw_models, dict) or not isinstance(raw_routing, dict):
        raise ValueError("runtime policy local_models and routing must be objects")
    local_models = {
        tier: validate_model(
            raw_models.get(tier),
            base["local_models"].get(tier, DEFAULT_MODELS[tier]),
            allow_unset=tier in OPTIONAL_LOCAL_TIERS,
        )
        for tier in LOCAL_TIERS
    }
    embedding_model = validate_model(
        payload.get("embedding_model"),
        base.get("embedding_model", DEFAULT_EMBEDDING_MODEL),
    )
    routing = {}
    for lane in ROUTING_LANES:
        tier = str(raw_routing.get(lane) or base["routing"][lane]).strip().lower()
        # Lanes pin to base tiers only: a lane must always resolve to a bound
        # model. Specialist tiers are chosen per request by the capability
        # router, never pinned to a lane.
        if tier not in BASE_LOCAL_TIERS:
            raise ValueError(
                "runtime routing lane %s must use: %s"
                % (lane, ", ".join(BASE_LOCAL_TIERS))
            )
        routing[lane] = tier
    return {
        "version": VERSION,
        "revision": max(0, int(payload.get("revision") or 0)),
        "local_models": local_models,
        "embedding_model": embedding_model,
        "routing": routing,
        "npu": normalize_npu(payload.get("npu"), base.get("npu")),
        "long_context_overflow": normalize_long_context_overflow(
            payload.get("long_context_overflow"), base.get("long_context_overflow"),
        ),
        "provider_models": normalize_provider_models(payload.get("provider_models")),
        "updated_ts": max(0, int(payload.get("updated_ts") or 0)),
        "source": str(payload.get("source") or "runtime policy")[:120],
    }


def bound_tiers(policy) -> tuple:
    """Tiers this policy actually binds to a model, in canonical order.

    An optional tier left unset is absent here, which is exactly what the
    capability router needs as its ``available`` set: an unbound specialist
    tier is never recommended and the request degrades to a base tier.
    """
    models = policy.get("local_models") if isinstance(policy, dict) else None
    if not isinstance(models, dict):
        return tuple(BASE_LOCAL_TIERS)
    return tuple(
        tier for tier in LOCAL_TIERS
        if str(models.get(tier) or "").strip()
    )


def npu_mode(capability, policy) -> str:
    """Effective accelerator mode for one capability; unknown means off."""
    capability = str(capability or "").strip().lower()
    if capability not in NPU_CAPABILITIES:
        return "off"
    section = policy.get("npu") if isinstance(policy, dict) else None
    if not isinstance(section, dict):
        return "off"
    override = str(section.get(capability) or "").strip().lower()
    mode = override or str(section.get("mode") or "off").strip().lower()
    return mode if mode in NPU_MODES else "off"


def disk_payload(policy: dict) -> dict:
    payload = {key: policy[key] for key in (
        "version", "revision", "local_models", "embedding_model", "routing", "npu",
        "long_context_overflow", "updated_ts", "source",
    )}
    # Written only once an operator maps a hosted provider's tier, so a
    # policy file that never used it keeps its existing shape.
    if policy.get("provider_models"):
        payload["provider_models"] = policy["provider_models"]
    return payload
