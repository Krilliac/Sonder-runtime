"""Pure runtime-policy rules (SPEC-3 Phase 2 extraction).

Moved verbatim from the root ``runtime_policy.py`` module, minus every
I/O concern. These functions read no environment and touch no files —
callers pass the environment mapping explicitly. The root module now
delegates here; behavior is unchanged.

The policy intentionally cannot configure cloud models, permissions,
filesystem roots, or credentials.
"""
from __future__ import annotations

import re

VERSION = 1
LOCAL_TIERS = ("fast", "code", "general")
ROUTING_LANES = ("router", "workbench", "autopilot", "fleet", "review")
DEFAULT_MODELS = {
    "fast": "qwen2.5:3b",
    "code": "sonder:latest",
    "general": "sonder:latest",
}
RESERVED_PERSONAL_MODEL = "sonder-personal:latest"
DEFAULT_ROUTING = {
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


def validate_model(value, fallback: str) -> str:
    model = str(value or fallback).strip()
    if not _MODEL_RE.fullmatch(model):
        raise ValueError("invalid local model name %r" % model)
    if is_cloud_name(model):
        raise ValueError("runtime policy local tiers cannot reference cloud models")
    if is_reserved_personal_alias(model):
        return RESERVED_PERSONAL_MODEL
    return model


def seed_model(env, tier: str) -> str:
    """Seed one tier's model from an explicit environment mapping."""
    configured = str(env.get("SONDER_%s" % tier.upper(), "") or "").strip()
    if tier == "code" and is_cloud_name(configured):
        configured = str(env.get("SONDER_CODE_LOCAL", "") or "").strip()
    if is_reserved_personal_alias(configured):
        configured = ""
    if configured and not is_cloud_name(configured):
        return validate_model(configured, DEFAULT_MODELS[tier])
    return DEFAULT_MODELS[tier]


def default_policy(env) -> dict:
    """Built-in policy seeded from an explicit environment mapping."""
    return {
        "version": VERSION,
        "revision": 0,
        "local_models": {tier: seed_model(env, tier) for tier in LOCAL_TIERS},
        "routing": dict(DEFAULT_ROUTING),
        "npu": dict(DEFAULT_NPU),
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
        tier: validate_model(raw_models.get(tier), base["local_models"][tier])
        for tier in LOCAL_TIERS
    }
    routing = {}
    for lane in ROUTING_LANES:
        tier = str(raw_routing.get(lane) or base["routing"][lane]).strip().lower()
        if tier not in LOCAL_TIERS:
            raise ValueError(
                "runtime routing lane %s must use: %s"
                % (lane, ", ".join(LOCAL_TIERS))
            )
        routing[lane] = tier
    return {
        "version": VERSION,
        "revision": max(0, int(payload.get("revision") or 0)),
        "local_models": local_models,
        "routing": routing,
        "npu": normalize_npu(payload.get("npu"), base.get("npu")),
        "updated_ts": max(0, int(payload.get("updated_ts") or 0)),
        "source": str(payload.get("source") or "runtime policy")[:120],
    }


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
    return {key: policy[key] for key in (
        "version", "revision", "local_models", "routing", "npu", "updated_ts",
        "source",
    )}
