"""Shared, hot-reloadable policy for local models and execution lanes.

Every Trilobite surface uses the same per-user file. The policy intentionally
cannot configure cloud models, permissions, roots, or credentials.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

import trilobite_paths


VERSION = 1
LOCAL_TIERS = ("fast", "code", "general")
ROUTING_LANES = ("router", "workbench", "autopilot", "fleet", "review")
DEFAULT_MODELS = {
    "fast": "qwen2.5:3b",
    "code": "trilobite:latest",
    "general": "trilobite:latest",
}
DEFAULT_ROUTING = {
    "router": "fast",
    "workbench": "code",
    "autopilot": "code",
    "fleet": "code",
    "review": "code",
}
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")
_LOCK = threading.RLock()


def policy_path() -> Path:
    override = os.environ.get("TRILOBITE_RUNTIME_POLICY", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(trilobite_paths.state_path("runtime_policy.json"))


def _is_cloud_name(value: str) -> bool:
    lowered = str(value or "").strip().lower()
    return "-cloud" in lowered or lowered.endswith(":cloud")


def _model(value, fallback: str) -> str:
    model = str(value or fallback).strip()
    if not _MODEL_RE.fullmatch(model):
        raise ValueError("invalid local model name %r" % model)
    if _is_cloud_name(model):
        raise ValueError("runtime policy local tiers cannot reference cloud models")
    return model


def _seed_model(env, tier: str) -> str:
    configured = str(env.get("LOCAL_LLM_%s" % tier.upper(), "") or "").strip()
    if tier == "code" and _is_cloud_name(configured):
        configured = str(env.get("LOCAL_LLM_CODE_LOCAL", "") or "").strip()
    if configured and not _is_cloud_name(configured):
        return _model(configured, DEFAULT_MODELS[tier])
    return DEFAULT_MODELS[tier]


def default_policy(env=None) -> dict:
    env = os.environ if env is None else env
    return {
        "version": VERSION,
        "revision": 0,
        "local_models": {
            tier: _seed_model(env, tier) for tier in LOCAL_TIERS
        },
        "routing": dict(DEFAULT_ROUTING),
        "updated_ts": 0,
        "source": "environment seed",
    }


def normalize(payload, defaults=None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("runtime policy must be a JSON object")
    # Environment variables seed a file only when it is first created. Once a
    # shared policy exists, normalization and recovery use stable built-ins so
    # separately launched surfaces cannot drift with their inherited env.
    base = default_policy(env={}) if defaults is None else defaults
    raw_models = payload.get("local_models") or {}
    raw_routing = payload.get("routing") or {}
    if not isinstance(raw_models, dict) or not isinstance(raw_routing, dict):
        raise ValueError("runtime policy local_models and routing must be objects")
    local_models = {
        tier: _model(raw_models.get(tier), base["local_models"][tier])
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
        "updated_ts": max(0, int(payload.get("updated_ts") or 0)),
        "source": str(payload.get("source") or "runtime policy")[:120],
    }


def _disk_payload(policy: dict) -> dict:
    return {key: policy[key] for key in (
        "version", "revision", "local_models", "routing", "updated_ts", "source"
    )}


def _write(policy: dict, path=None) -> Path:
    path = policy_path() if path is None else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("%s.tmp-%s" % (path.name, uuid.uuid4().hex))
    try:
        temporary.write_text(
            json.dumps(_disk_payload(policy), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def load(create=True) -> dict:
    path = policy_path()
    with _LOCK:
        if not path.exists():
            policy = default_policy()
            if create:
                _write(policy, path)
            return {**policy, "path": str(path), "error": ""}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            policy = normalize(raw)
            error = ""
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            policy = default_policy(env={})
            error = "%s: %s" % (type(exc).__name__, exc)
        return {**policy, "path": str(path), "error": error}


def update(local_models=None, routing=None, reset=False, source="user update") -> dict:
    with _LOCK:
        current = load(create=True)
        if current.get("error") and not reset:
            raise ValueError(
                "runtime policy is invalid; use reset before updating: %s"
                % current["error"]
            )
        base = default_policy(env={}) if reset else current
        candidate = {
            **base,
            "local_models": dict(base["local_models"]),
            "routing": dict(base["routing"]),
        }
        if local_models:
            if not isinstance(local_models, dict):
                raise ValueError("local_models update must be a JSON object")
            unknown = set(local_models) - set(LOCAL_TIERS)
            if unknown:
                raise ValueError("unknown local tier(s): %s" % ", ".join(sorted(unknown)))
            candidate["local_models"].update(local_models)
        if routing:
            if not isinstance(routing, dict):
                raise ValueError("routing update must be a JSON object")
            unknown = set(routing) - set(ROUTING_LANES)
            if unknown:
                raise ValueError("unknown routing lane(s): %s" % ", ".join(sorted(unknown)))
            candidate["routing"].update(routing)
        candidate["revision"] = int(current.get("revision") or 0) + 1
        candidate["updated_ts"] = int(time.time())
        candidate["source"] = str(source or "user update")[:120]
        normalized = normalize(candidate, defaults=default_policy(env={}))
        _write(normalized)
        return load(create=False)


def route_tier(lane: str, policy=None, fallback="code") -> str:
    lane = str(lane or "").strip().lower()
    policy = load(create=True) if policy is None else policy
    tier = str((policy.get("routing") or {}).get(lane) or fallback).strip().lower()
    return tier if tier in LOCAL_TIERS else fallback


def format_policy(policy=None) -> str:
    policy = load(create=True) if policy is None else policy
    lines = [
        "trilobite local runtime policy",
        "  path: %s" % policy.get("path", policy_path()),
        "  revision: %s | source: %s" % (
            policy.get("revision", 0), policy.get("source", ""),
        ),
    ]
    if policy.get("error"):
        lines.append("  ERROR: %s (safe defaults active)" % policy["error"])
    lines.append("  local models:")
    for tier in LOCAL_TIERS:
        lines.append("    %s: %s" % (tier, policy["local_models"][tier]))
    lines.append("  execution lanes:")
    for lane in ROUTING_LANES:
        tier = policy["routing"][lane]
        lines.append("    %s: %s -> %s" % (
            lane, tier, policy["local_models"][tier],
        ))
    lines.append("  cloud tiers remain separate explicit opt-in configuration")
    return "\n".join(lines)
