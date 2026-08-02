"""Policy-aware host facade for the NPU utility accelerator.

This is the only integration surface the server and embeddings use. It is
stdlib-only, never raises into callers, and enforces the host side of the
accelerator contract:

- policy states off/shadow/prefer per capability (host-owned runtime policy);
- deterministic host-computed routing features (never raw prompt text) with a
  versioned identity the manifest must match;
- allowlist validation of every accelerator response before use;
- exact-identity gating for embedding acceleration so vector spaces can never
  mix and a different embedder is never silently substituted;
- bounded, redacted status/diagnostics/activity reporting (no prompt text, no
  vectors, no logits — counts, enums, and hashes only).

Unavailability always means "use the existing local behavior". Nothing here
can reach cloud tiers, permissions, roots, credentials, or executable paths.
"""
from __future__ import annotations

import re
import threading

import activity_tracker
import npu_broker
import npu_contract
import npu_manifest
import runtime_policy


FEATURES_ID = "exec-route-features-v1"
FEATURES_DIM = 16
# A prefer-mode decision needs a confidently scored winner; anything weaker
# falls back to the existing local Ollama router.
MIN_PREFER_SCORE = 0.6
MIN_PREFER_MARGIN = 0.2

_ACTION_RE = re.compile(
    r"\b(add|audit|build|clean|configure|convert|create|debug|deploy|diagnose|"
    r"document|edit|fix|generate|implement|improve|inspect|install|migrate|"
    r"optimize|patch|refactor|rename|repair|review|rewrite|run|scan|test|"
    r"update|upgrade|validate|verify|write)\b"
)
_SEQUENCE_RE = re.compile(r"\b(then|after|before|first|next|finally|once)\b")
_PLAN_RE = re.compile(r"\b(plan|design|architect|roadmap)\b")
_TEST_RE = re.compile(r"\b(test|tests|validate|validation|verify|check)\b")
_CHANGE_RE = re.compile(r"\b(fix|implement|refactor|repair|patch|rewrite)\b")
_INSPECT_RE = re.compile(r"\b(inspect|diagnose|investigate|analyze|audit)\b")
_PATH_RE = re.compile(r"[/\\]|\.[a-z0-9]{1,4}\b|\brepo\b|\brepository\b")
_QUANTIFIER_RE = re.compile(r"\b(all|every|entire|whole)\b")

# Mutable runtime state survives helper live-reload, like activity spans do.
if "_STATE_LOCK" not in globals():
    _STATE_LOCK = threading.Lock()
if "_MANIFEST_CACHE" not in globals():
    _MANIFEST_CACHE = {"signature": None, "rows": []}
if "_SHADOW" not in globals():
    _SHADOW = {"agree": 0, "disagree": 0, "errors": 0}


def reset_for_tests():
    with _STATE_LOCK:
        _MANIFEST_CACHE["signature"] = None
        _MANIFEST_CACHE["rows"] = []
        _SHADOW.update(agree=0, disagree=0, errors=0)


def _policy():
    try:
        return runtime_policy.load(create=False)
    except Exception:
        return None


def _mode(capability, policy=None):
    try:
        return runtime_policy.npu_mode(capability, policy)
    except Exception:
        return "off"


def _event(kind, **fields):
    try:
        activity_tracker.record_event(kind, **fields)
    except Exception:
        pass


def _manifest_signature(base):
    try:
        import os

        entries = []
        with os.scandir(base) as scan:
            for entry in scan:
                if entry.name.endswith(".json"):
                    stat = entry.stat()
                    entries.append((entry.name, stat.st_mtime_ns, stat.st_size))
        return (str(base), tuple(sorted(entries)))
    except OSError:
        return (str(base), None)


def _manifests():
    base = npu_manifest.manifest_dir()
    signature = _manifest_signature(base)
    with _STATE_LOCK:
        if _MANIFEST_CACHE["signature"] == signature:
            return _MANIFEST_CACHE["rows"]
    rows = npu_manifest.load_manifests(base) if signature[1] is not None else []
    with _STATE_LOCK:
        _MANIFEST_CACHE["signature"] = signature
        _MANIFEST_CACHE["rows"] = rows
    return rows


def _active(operation):
    return npu_manifest.active_manifest(operation, _manifests())


def _clip(value, ceiling):
    return round(min(1.0, max(0.0, value / ceiling)), 4)


def route_features(text) -> list:
    """Deterministic, bounded features for ambiguous execution routing.

    The raw prompt never crosses the process boundary for routing: only this
    fixed-length numeric vector does. The identity string is part of the
    manifest contract; any change here requires a new FEATURES_ID.
    """
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    lowered = value.lower()
    words = lowered.split()
    sentences = [part for part in re.split(r"[.!?]+", lowered) if part.strip()]
    actions = set(_ACTION_RE.findall(lowered))
    starts_with_action = bool(words and _ACTION_RE.fullmatch(words[0]))
    return [
        _clip(len(value), 2000.0),
        _clip(len(words), 300.0),
        _clip(len(actions), 6.0),
        _clip(len(_SEQUENCE_RE.findall(lowered)), 4.0),
        _clip(lowered.count(","), 6.0),
        _clip(len(re.findall(r"\band\b", lowered)), 5.0),
        1.0 if _PLAN_RE.search(lowered) else 0.0,
        1.0 if _TEST_RE.search(lowered) else 0.0,
        1.0 if _CHANGE_RE.search(lowered) else 0.0,
        1.0 if _INSPECT_RE.search(lowered) else 0.0,
        1.0 if "?" in value else 0.0,
        1.0 if starts_with_action else 0.0,
        1.0 if _PATH_RE.search(lowered) else 0.0,
        1.0 if _QUANTIFIER_RE.search(lowered) else 0.0,
        _clip(len(sentences), 5.0),
        1.0 if any(char.isdigit() for char in value) else 0.0,
    ]


def _routing_call(prompt):
    """Shared prefer/shadow path: returns (decision, provider) or (None, why)."""
    manifest = _active("routing")
    if manifest is None:
        return None, "no_manifest"
    declared = manifest.get("input") or {}
    features = route_features(prompt)
    if (
        declared.get("identity") != FEATURES_ID
        or declared.get("dimension") != len(features)
    ):
        return None, "identity_mismatch"
    broker = npu_broker.get_broker()
    try:
        response = broker.call(
            manifest, {"kind": "routing", "features": features},
        )
    except npu_broker.NpuUnavailable as exc:
        return None, exc.reason
    except Exception:
        return None, "internal"
    try:
        validated = npu_contract.validate_route_scores(response)
    except ValueError:
        return None, "invalid"
    scores = validated["scores"]
    winner = max(npu_contract.ROUTE_MODES, key=lambda mode: scores[mode])
    margin = abs(scores["autopilot"] - scores["workbench"])
    decision = {
        "scores": scores,
        "winner": winner,
        "margin": round(margin, 4),
        "provider": str(response.get("provider") or "")[:24],
        "simulated": bool(response.get("simulated")),
        "reason_code": validated["reason_code"],
    }
    return decision, ""


def route_decide(prompt):
    """Prefer-mode decision for the ambiguous execution band, or None.

    Deterministic host cues never reach this function; the caller only asks
    when its own classifier said "decide". Returns a dict shaped like the
    local router model's decision so the host dispatch path stays identical.
    """
    policy = _policy()
    if _mode("routing", policy) != "prefer":
        return None
    decision, why = _routing_call(prompt)
    if decision is None:
        _event("npu_fallback", capability="routing", reason=why)
        return None
    score = decision["scores"][decision["winner"]]
    if (
        decision["reason_code"] != "score_margin"
        or decision["margin"] < MIN_PREFER_MARGIN
        or score < MIN_PREFER_SCORE
    ):
        _event("npu_fallback", capability="routing", reason="low_confidence")
        return None
    _event(
        "npu_route",
        mode=decision["winner"],
        provider=decision["provider"],
        simulated=decision["simulated"],
        margin=decision["margin"],
    )
    return {
        "mode": decision["winner"],
        "tier": runtime_policy.route_tier(
            decision["winner"], policy, fallback="code",
        ),
        "reason": "npu accelerator (%s): score margin %.2f" % (
            decision["provider"] or "unknown", decision["margin"],
        ),
        "confidence": round(score, 4),
        "source": "npu accelerator",
    }


def route_shadow(prompt, baseline):
    """Shadow-mode comparison; never changes behavior, returns None always."""
    if _mode("routing") != "shadow":
        return None
    decision, why = _routing_call(prompt)
    if decision is None:
        with _STATE_LOCK:
            _SHADOW["errors"] += 1
        _event("npu_route_shadow", ok=False, reason=why)
        return None
    baseline_mode = str((baseline or {}).get("mode") or "")
    agree = baseline_mode == decision["winner"]
    with _STATE_LOCK:
        _SHADOW["agree" if agree else "disagree"] += 1
    _event(
        "npu_route_shadow",
        ok=True,
        agree=agree,
        npu_mode=decision["winner"],
        baseline_mode=baseline_mode,
        provider=decision["provider"],
        simulated=decision["simulated"],
        margin=decision["margin"],
    )
    return None


def _embedding_call(manifest, text):
    limit = int((manifest.get("limits") or {}).get("max_text_chars") or 0)
    prompt = str(text or "")
    if limit > 0:
        prompt = prompt[:limit]
    broker = npu_broker.get_broker()
    response = broker.call(manifest, {"kind": "embedding", "texts": [prompt]})
    vectors = response.get("vectors")
    if not isinstance(vectors, list) or len(vectors) != 1:
        raise ValueError("embedding response must carry exactly one vector")
    vector = npu_contract.validate_vector(
        vectors[0], int(manifest.get("dimension") or 0),
    )
    return {
        "vector": vector,
        "provider": str(response.get("provider") or "")[:24],
        "ep": str(response.get("ep") or "")[:48],
        "simulated": bool(response.get("simulated")),
        "manifest_hash": str(manifest.get("manifest_hash") or ""),
    }


def embeddings_active() -> str:
    """Effective embeddings accelerator mode: off, shadow, or prefer."""
    return _mode("embeddings")


def embed_for_space(text, model_identity, revision):
    """Accelerated embedding only for the exact declared vector space.

    The manifest must declare ``space`` pinning the same model identity and
    serving revision the legacy embedder would use right now. Same-model CPU
    fallback inside the worker keeps this identity; anything else returns None
    so the caller's existing embedder (and its lexical fail-soft) is used.
    """
    if _mode("embeddings") != "prefer":
        return None
    manifest = _active("embedding")
    if manifest is None:
        return None
    space = manifest.get("space")
    if not space:
        return None
    if (
        space.get("model") != str(model_identity or "")
        or space.get("revision") != str(revision or "")
    ):
        _event("npu_fallback", capability="embeddings", reason="space_mismatch")
        return None
    try:
        result = _embedding_call(manifest, text)
    except npu_broker.NpuUnavailable as exc:
        _event("npu_fallback", capability="embeddings", reason=exc.reason)
        return None
    except (TypeError, ValueError):
        _event("npu_fallback", capability="embeddings", reason="invalid")
        return None
    except Exception:
        _event("npu_fallback", capability="embeddings", reason="internal")
        return None
    _event(
        "npu_embed",
        provider=result["provider"],
        simulated=result["simulated"],
        dimension=len(result["vector"]),
    )
    return result


def embed_shadow(text, model_identity, revision):
    """Shadow-mode embedding exercise: health/latency only, result discarded."""
    if _mode("embeddings") != "shadow":
        return None
    manifest = _active("embedding")
    if manifest is None:
        return None
    try:
        result = _embedding_call(manifest, text)
    except npu_broker.NpuUnavailable as exc:
        _event("npu_embed_shadow", ok=False, reason=exc.reason)
        return None
    except Exception:
        _event("npu_embed_shadow", ok=False, reason="invalid")
        return None
    _event(
        "npu_embed_shadow",
        ok=True,
        provider=result["provider"],
        simulated=result["simulated"],
    )
    return None


def _manifest_summary(manifest):
    if manifest is None:
        return None
    summary = {
        "name": str(manifest.get("name") or "")[:64],
        "operation": str(manifest.get("operation") or ""),
        "hash8": str(manifest.get("manifest_hash") or "")[:8],
        "providers": list(manifest.get("providers") or []),
        "error": str(manifest.get("error") or "")[:200],
    }
    if manifest.get("operation") == "embedding":
        summary["dimension"] = manifest.get("dimension")
        summary["space_pinned"] = bool(manifest.get("space"))
    else:
        summary["input_identity"] = str(
            (manifest.get("input") or {}).get("identity") or ""
        )
    return summary


def status(probe=False) -> dict:
    """Bounded accelerator state: detected vs runtime-ready vs enabled vs
    healthy, plus circuit/latency/fallback counters. Never carries text."""
    policy = _policy()
    modes = {
        capability: _mode(capability, policy)
        for capability in runtime_policy.NPU_CAPABILITIES
    }
    enabled = any(mode != "off" for mode in modes.values())
    rows = _manifests()
    active = {
        "routing": _active("routing"),
        "embedding": _active("embedding"),
    }
    broker = npu_broker.get_broker()
    if probe and enabled:
        targets = [manifest for manifest in active.values() if manifest]
        if targets:
            try:
                broker.ensure_warm(targets)
            except Exception:
                pass
    broker_status = broker.status()
    providers = broker_status.get("providers") or []
    probed = bool(providers)
    detected = None
    runtime_ready = None
    if probed:
        ready_ids = {
            row.get("id") for row in providers if row.get("runtime_ready")
        }
        detected = any(
            row.get("detected")
            and row.get("id") in npu_contract.NPU_CLASS_PROVIDERS
            for row in providers
        )
        runtime_ready = any(
            manifest and set(manifest.get("providers") or []) & ready_ids
            for manifest in active.values()
        )
    circuit = broker_status.get("circuit") or {}
    healthy = (
        circuit.get("state") == "closed"
        and not circuit.get("consecutive_failures")
    )
    with _STATE_LOCK:
        shadow = dict(_SHADOW)
    return {
        "modes": modes,
        "enabled": enabled,
        "detected": detected,
        "runtime_ready": runtime_ready,
        "healthy": healthy,
        "features_id": FEATURES_ID,
        "manifest_count": len(rows),
        "manifest_errors": sum(1 for row in rows if row.get("error")),
        "manifests": {
            "routing": _manifest_summary(active["routing"]),
            "embedding": _manifest_summary(active["embedding"]),
        },
        "shadow": shadow,
        "broker": broker_status,
    }


def _flag(value):
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def diagnostics_line(state=None) -> str:
    """One bounded line for the server diagnostics report."""
    state = status() if state is None else state
    modes = state["modes"]
    if not state["enabled"]:
        probed = state["detected"] is not None
        return "off (policy; %s)" % (
            "detected=%s" % _flag(state["detected"]) if probed else "not probed"
        )
    broker_state = state["broker"]
    return (
        "routing=%s embeddings=%s detected=%s runtime-ready=%s healthy=%s "
        "worker=%s circuit=%s p95=%sms"
        % (
            modes.get("routing"),
            modes.get("embeddings"),
            _flag(state["detected"]),
            _flag(state["runtime_ready"]),
            _flag(state["healthy"]),
            (broker_state.get("worker") or {}).get("state", "cold"),
            (broker_state.get("circuit") or {}).get("state", "closed"),
            (broker_state.get("latency_ms") or {}).get("p95", 0),
        )
    )


def format_status(state=None) -> str:
    """Human-readable accelerator status for the npu_status tool."""
    state = status() if state is None else state
    modes = state["modes"]
    broker_state = state["broker"]
    worker = broker_state.get("worker") or {}
    circuit = broker_state.get("circuit") or {}
    latency = broker_state.get("latency_ms") or {}
    hello = broker_state.get("hello") or {}
    lines = [
        "sonder npu accelerator",
        "  role: utility accelerator below local tiers (never a model tier)",
        "  policy: routing=%s embeddings=%s" % (
            modes.get("routing"), modes.get("embeddings"),
        ),
        "  state: detected=%s runtime-ready=%s enabled=%s healthy=%s" % (
            _flag(state["detected"]), _flag(state["runtime_ready"]),
            "yes" if state["enabled"] else "no", _flag(state["healthy"]),
        ),
        "  worker: %s (spawns=%s idle-unloads=%s rss-evictions=%s rss=%sMB)" % (
            worker.get("state", "cold"), worker.get("spawns", 0),
            worker.get("idle_unloads", 0), worker.get("rss_evictions", 0),
            worker.get("rss_mb", 0),
        ),
        "  circuit: %s (opens=%s cooldown=%ss)" % (
            circuit.get("state", "closed"), circuit.get("opens", 0),
            circuit.get("cooldown_remaining_s", 0),
        ),
        "  latency: last=%sms p50=%sms p95=%sms over %s call(s)" % (
            latency.get("last", 0), latency.get("p50", 0),
            latency.get("p95", 0), latency.get("count", 0),
        ),
        "  shadow: agree=%s disagree=%s errors=%s" % (
            state["shadow"].get("agree", 0), state["shadow"].get("disagree", 0),
            state["shadow"].get("errors", 0),
        ),
    ]
    if hello.get("ort_version"):
        lines.append("  onnxruntime: %s" % hello["ort_version"])
    elif hello.get("ort_error"):
        lines.append("  onnxruntime: unavailable (%s)" % hello["ort_error"])
    providers = broker_state.get("providers") or []
    if providers:
        lines.append("  providers:")
        for row in providers:
            lines.append(
                "    %-8s detected=%s ready=%s %s" % (
                    row.get("id", "?"), _flag(bool(row.get("detected"))),
                    _flag(bool(row.get("runtime_ready"))),
                    str(row.get("reason") or "")[:80],
                )
            )
    else:
        lines.append("  providers: not probed (worker cold)")
    for operation in ("routing", "embedding"):
        summary = state["manifests"].get(operation)
        if summary is None:
            lines.append("  %s model: none configured" % operation)
            continue
        detail = "hash=%s providers=%s" % (
            summary["hash8"], ",".join(summary["providers"]),
        )
        if summary.get("error"):
            detail += " ERROR: %s" % summary["error"]
        lines.append("  %s model: %s (%s)" % (
            operation, summary["name"], detail,
        ))
    fallbacks = broker_state.get("fallbacks") or {}
    if fallbacks:
        rendered = ", ".join(
            "%s=%s" % (key, fallbacks[key]) for key in sorted(fallbacks)
        )
        lines.append("  fallbacks: %s" % rendered)
    if broker_state.get("last_error"):
        lines.append("  last error: %s" % broker_state["last_error"])
    lines.append(
        "  boundary: NPU failure falls back to existing local behavior; "
        "cloud is never a fallback"
    )
    return "\n".join(lines)


def warm_if_enabled():
    """Best-effort warmup trigger used by explicit probes; never blocks."""
    policy = _policy()
    if all(
        _mode(capability, policy) == "off"
        for capability in runtime_policy.NPU_CAPABILITIES
    ):
        return False
    targets = [
        manifest
        for manifest in (_active("routing"), _active("embedding"))
        if manifest
    ]
    if not targets:
        return False
    try:
        return bool(npu_broker.get_broker().ensure_warm(targets))
    except Exception:
        return False
