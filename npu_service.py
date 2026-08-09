"""Policy-aware host facade for the NPU utility accelerator.

This is the only integration surface the server and embeddings use. It is
stdlib-only, never raises into callers, and enforces the host side of the
accelerator contract:

- policy states off/shadow/prefer per capability (host-owned runtime policy);
- deterministic host-computed routing features (never raw prompt text) with a
  versioned identity the manifest must match;
- allowlist validation of every accelerator response before use;
- exact model/revision/dimension gating for embedding acceleration so vector
  spaces can never mix; simulated and truncated inputs never substitute;
- bounded, redacted status/diagnostics/activity reporting (no prompt text, no
  vectors, no logits — counts, enums, and hashes only).

Unavailability always means "use the existing local behavior". The protocol
has no cloud/permission/credential operations; the worker is environment-
scrubbed failure isolation, not an OS sandbox, and runs as the same user.
"""
from __future__ import annotations

import json
import os
import re
import threading

import activity_tracker
import npu_broker
import npu_contract
import npu_manifest
import npu_providers
import runtime_policy
import sonder_paths
import system_profile


FEATURES_ID = "exec-route-features-v2"
FEATURES_DIM = 36
LEGACY_FEATURES_ID = "exec-route-features-v1"
LEGACY_FEATURES_DIM = 16
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

# v2 semantic classes. v1's shape/count features cannot see WHICH verbs and
# targets appear, which is what the LLM router baseline actually keys on
# (measured 2026-08: distilled v1 agreement 0.665 vs 0.626 majority). These
# stay deterministic and bounded; raw text still never crosses the process
# boundary.
_VERB_CLASS_RES = (
    re.compile(r"\b(add|build|create|generate|implement|make|scaffold|write)\b"),
    re.compile(r"\b(correct|debug|fix|patch|repair)\b"),
    re.compile(
        r"\b(analyze|audit|benchmark|check|diagnose|inspect|investigate|"
        r"review|scan|test|validate|verify)\b"
    ),
    re.compile(
        r"\b(clean|convert|edit|improve|migrate|modify|move|optimize|"
        r"refactor|rename|rewrite|update|upgrade)\b"
    ),
    re.compile(r"\b(describe|document|explain|summarize)\b"),
    re.compile(r"\b(compile|configure|deploy|execute|install|run|ship)\b"),
    re.compile(r"\b(find|list|open|read|search|view)\b"),
    re.compile(r"\b(delete|remove)\b"),
)
_TARGET_CLASS_RES = (
    re.compile(
        r"\b(api|build|cli|code|function|library|module|package|repo|"
        r"repository|script|scripts|test|tests|tool|tools)\b"
    ),
    re.compile(r"\b(changelog|doc|docs|document|presentation|readme|report)\b"),
    re.compile(
        r"\b(animation|audio|chart|dashboard|graphic|icon|image|logo|music|"
        r"scene|sound|sprite|texture|ui|webpage|website)\b"
    ),
    re.compile(
        r"\b(app|application|config|data|directory|file|files|folder|model|"
        r"path|pipeline|project|system|workspace)\b"
    ),
)
_NUMBERED_STEP_RE = re.compile(r"\b\d+[).:]\s")

# Mutable runtime state survives helper live-reload, like activity spans do.
if "_STATE_LOCK" not in globals():
    _STATE_LOCK = threading.Lock()
if "_MANIFEST_CACHE" not in globals():
    _MANIFEST_CACHE = {"signature": None, "rows": []}
if "_SHADOW" not in globals():
    _SHADOW = {"agree": 0, "disagree": 0, "errors": 0}
if "_LEDGER" not in globals():
    _LEDGER = {"loaded": False, "recent": [], "agree": 0, "disagree": 0,
               "errors": 0}
if "_LAST_FALLBACK" not in globals():
    _LAST_FALLBACK = {
        "known": False,
        "capability": "unknown",
        "reason": "unknown",
        "operation_mode": "unknown",
        "fallback_handler": "unknown",
        "handler_state": "unknown",
        "count": 0,
    }
if "_FALLBACK_LOCAL" not in globals():
    _FALLBACK_LOCAL = threading.local()

# Promoting routing to prefer is an operator decision; these thresholds only
# gate the ADVISORY that live evidence supports it. The ledger survives
# restarts so shadow evidence accumulates across sessions.
SHADOW_ADVISORY_MIN_N = 50
SHADOW_ADVISORY_MIN_AGREEMENT = 0.85
_LEDGER_RECENT_CAP = 500


def reset_for_tests():
    with _STATE_LOCK:
        _MANIFEST_CACHE["signature"] = None
        _MANIFEST_CACHE["rows"] = []
        _SHADOW.update(agree=0, disagree=0, errors=0)
        _LEDGER.update(loaded=False, recent=[], agree=0, disagree=0, errors=0)
        _LAST_FALLBACK.update(
            known=False,
            capability="unknown",
            reason="unknown",
            operation_mode="unknown",
            fallback_handler="unknown",
            handler_state="unknown",
            count=0,
        )
        _FALLBACK_LOCAL.pending = {}


def _ledger_path() -> str:
    return sonder_paths.state_path(
        "npu-shadow-ledger.json", "SONDER_NPU_SHADOW_LEDGER",
    )


def _ledger_load_locked():
    if _LEDGER["loaded"]:
        return
    _LEDGER["loaded"] = True
    try:
        with open(_ledger_path(), "r", encoding="utf-8") as stream:
            raw = json.load(stream)
        recent = [
            1 if item else 0 for item in (raw.get("recent") or [])
        ][-_LEDGER_RECENT_CAP:]
        _LEDGER["recent"] = recent
        for key in ("agree", "disagree", "errors"):
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                _LEDGER[key] = max(0, value)
    except (OSError, TypeError, ValueError):
        pass


def _ledger_record(outcome):
    """Persist one shadow outcome ('agree'/'disagree'/'error'); fail-soft."""
    with _STATE_LOCK:
        _ledger_load_locked()
        if outcome in ("agree", "disagree"):
            _LEDGER["recent"].append(1 if outcome == "agree" else 0)
            del _LEDGER["recent"][:-_LEDGER_RECENT_CAP]
            _LEDGER[outcome] += 1
        else:
            _LEDGER["errors"] += 1
        snapshot = {
            "recent": list(_LEDGER["recent"]),
            "agree": _LEDGER["agree"],
            "disagree": _LEDGER["disagree"],
            "errors": _LEDGER["errors"],
        }
    try:
        path = _ledger_path()
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as stream:
            json.dump(snapshot, stream)
        os.replace(temp, path)
    except OSError:
        pass


def shadow_advisory() -> dict:
    """Live routing shadow evidence and the promotion advisory it supports."""
    with _STATE_LOCK:
        _ledger_load_locked()
        recent = list(_LEDGER["recent"])
        totals = {
            "agree": _LEDGER["agree"],
            "disagree": _LEDGER["disagree"],
            "errors": _LEDGER["errors"],
        }
    window = len(recent)
    agreement = (sum(recent) / window) if window else None
    supported = (
        window >= SHADOW_ADVISORY_MIN_N
        and agreement is not None
        and agreement >= SHADOW_ADVISORY_MIN_AGREEMENT
    )
    if supported:
        advisory = (
            "live shadow agreement supports routing prefer "
            "(runtime_policy_update npu_json '{\"routing\": \"prefer\"}')"
        )
    elif window >= SHADOW_ADVISORY_MIN_N:
        advisory = "live shadow agreement below the prefer threshold"
    else:
        advisory = "insufficient live shadow decisions (%d of %d)" % (
            window, SHADOW_ADVISORY_MIN_N,
        )
    return {
        "window": window,
        "agreement": round(agreement, 4) if agreement is not None else None,
        "totals": totals,
        "threshold": SHADOW_ADVISORY_MIN_AGREEMENT,
        "min_decisions": SHADOW_ADVISORY_MIN_N,
        "prefer_supported": supported,
        "advisory": advisory,
    }


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


def _handler_for_execution(provider="", accelerated=False) -> str:
    if accelerated is True:
        return "npu"
    if str(provider or "").strip().lower() in {"cpu", "cpu-sim"}:
        return "cpu"
    return "host"


def _record_fallback(capability, reason, *, handler="ollama") -> dict:
    projected = {
        "known": True,
        "capability": npu_contract.fallback_capability(capability),
        "reason": npu_contract.fallback_reason(reason),
        "operation_mode": "execution",
        "fallback_handler": npu_contract.fallback_handler(handler),
        "handler_state": "pending",
    }
    with _STATE_LOCK:
        count = int(_LAST_FALLBACK.get("count") or 0) + 1
        _LAST_FALLBACK.update(projected, count=count)
    pending = dict(getattr(_FALLBACK_LOCAL, "pending", {}))
    pending[projected["capability"]] = dict(projected, count=count)
    _FALLBACK_LOCAL.pending = pending
    _event("npu_fallback", **projected)
    return dict(projected, count=count)


def record_fallback_handler(capability, handler, handled) -> None:
    """Record the bounded result of the host fallback, never its content."""
    capability = npu_contract.fallback_capability(capability)
    handler = npu_contract.fallback_handler(handler)
    state = "handled" if handled is True else "failed"
    pending = dict(getattr(_FALLBACK_LOCAL, "pending", {}))
    fallback = pending.pop(capability, None)
    _FALLBACK_LOCAL.pending = pending
    reason = (fallback or {}).get("reason", "unknown")
    with _STATE_LOCK:
        if (
            fallback is not None
            and _LAST_FALLBACK.get("capability") == capability
            and _LAST_FALLBACK.get("count") == fallback.get("count")
        ):
            _LAST_FALLBACK.update(
                fallback_handler=handler,
                handler_state=state,
            )
    _event(
        "npu_fallback_handled",
        capability=capability,
        reason=npu_contract.fallback_reason(reason),
        operation_mode="execution",
        fallback_handler=handler,
        handler_state=state,
        ok=handled is True,
    )


def _record_capability_event(kind, capability, *, reason="unknown", **fields):
    provider = fields.get("provider", "")
    accelerated = fields.get("accelerated") is True
    operation_mode = "shadow" if kind.endswith("_shadow") else "execution"
    handler = (
        "ollama"
        if operation_mode == "shadow"
        else _handler_for_execution(provider, accelerated)
    )
    _event(
        kind,
        capability=npu_contract.fallback_capability(capability),
        reason=npu_contract.fallback_reason(reason),
        operation_mode=operation_mode,
        fallback_handler=handler,
        handler_state=(
            "observed" if operation_mode == "shadow" else "handled"
        ),
        **fields,
    )


def routing_active() -> str:
    """Effective routing accelerator policy: off, shadow, or prefer."""
    return _mode("routing")


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


def route_features_v2(text) -> list:
    """v1 features plus deterministic semantic verb/target-class features.

    Dims 0-15 are exactly ``route_features``. Dims 16-35 add bounded class
    counts and structure cues. The identity string is part of the manifest
    contract; any change here requires a new FEATURES_ID.
    """
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    lowered = value.lower()
    sentences = [part.strip() for part in re.split(r"[.!?;]+", lowered)
                 if part.strip()]
    words = lowered.split()
    verb_counts = [len(rx.findall(lowered)) for rx in _VERB_CLASS_RES]
    target_counts = [len(rx.findall(lowered)) for rx in _TARGET_CLASS_RES]
    verify_rx = _VERB_CLASS_RES[2]
    imperative = [
        sentence for sentence in sentences
        if sentence and _ACTION_RE.match(sentence.split()[0])
    ]
    max_sentence_words = max(
        (len(sentence.split()) for sentence in sentences), default=0,
    )
    avg_word_len = (
        sum(len(word) for word in words) / len(words) if words else 0.0
    )
    return route_features(text) + [
        *(_clip(count, 3.0) for count in verb_counts),
        *(_clip(count, 3.0) for count in target_counts),
        1.0 if _NUMBERED_STEP_RE.search(value) else 0.0,
        1.0 if sentences and verify_rx.search(sentences[-1]) else 0.0,
        _clip(sum(1 for count in verb_counts if count), 8.0),
        _clip(sum(1 for count in target_counts if count), 4.0),
        _clip(max_sentence_words, 40.0),
        _clip(lowered.count(";") + lowered.count(":"), 4.0),
        round(len(imperative) / len(sentences), 4) if sentences else 0.0,
        _clip(avg_word_len, 8.0),
    ]


# Manifest ``input.identity`` selects the feature contract; unknown identities
# are rejected in _routing_call so a stale manifest can never feed mismatched
# features to a model.
FEATURE_CONTRACTS = {
    LEGACY_FEATURES_ID: (LEGACY_FEATURES_DIM, route_features),
    FEATURES_ID: (FEATURES_DIM, route_features_v2),
}


def _execution_provenance(response, manifest) -> dict:
    """Validate that worker provider claims are internally consistent."""
    provider = str(response.get("provider") or "").strip().lower()
    allowlist = list(manifest.get("providers") or [])
    if provider not in npu_contract.PROVIDER_IDS or provider not in allowlist:
        raise ValueError("response has an unknown provider")
    expected_ep = (
        npu_providers.SIMULATOR_EP
        if provider == "cpu-sim"
        else npu_providers.PROVIDER_EPS.get(provider, "")
    )
    ep = str(response.get("ep") or "")
    chain = response.get("ep_chain")
    if (
        not expected_ep
        or ep != expected_ep
        or not isinstance(chain, list)
        or not chain
        or len(chain) > 4
        or any(not isinstance(item, str) for item in chain)
        or chain[0] != expected_ep
    ):
        raise ValueError("response has inconsistent execution providers")
    for active_ep in chain:
        active_provider = npu_providers.provider_id_for_ep(active_ep)
        if not active_provider or active_provider not in allowlist:
            raise ValueError("response exposed an unallowlisted provider")
    if chain != [expected_ep]:
        raise ValueError("response did not bind one exclusive provider")
    simulated = response.get("simulated")
    if not isinstance(simulated, bool):
        raise ValueError("response omitted boolean simulator provenance")
    if simulated != (provider == "cpu-sim"):
        raise ValueError("response has inconsistent simulator provenance")
    cpu_fallback_disabled = response.get("cpu_fallback_disabled")
    if not isinstance(cpu_fallback_disabled, bool):
        raise ValueError("response omitted boolean CPU fallback provenance")
    npu_attested = response.get("npu_attested")
    if not isinstance(npu_attested, bool):
        raise ValueError("response omitted NPU attestation")
    if npu_attested and provider not in {"openvino", "qnn"}:
        raise ValueError("response has unsupported NPU device attestation")
    accelerated = npu_attested
    if provider in npu_contract.NPU_CLASS_PROVIDERS and (
        not cpu_fallback_disabled
    ):
        raise ValueError("NPU response permits ambiguous CPU fallback")
    if provider in {"cpu", "cpu-sim"} and cpu_fallback_disabled:
        raise ValueError("CPU response has inconsistent fallback provenance")
    ep_fallback = response.get("ep_fallback")
    if not isinstance(ep_fallback, bool):
        raise ValueError("response omitted boolean EP fallback provenance")
    if ep_fallback != (provider != allowlist[0]):
        raise ValueError("response has inconsistent fallback provenance")
    return {
        "provider": provider,
        "ep": ep,
        "ep_chain": list(chain),
        "accelerated": accelerated,
        "ep_fallback": ep_fallback,
        "cpu_fallback_disabled": cpu_fallback_disabled,
        "simulated": simulated,
        "npu_attested": npu_attested,
    }


def _routing_call(prompt):
    """Shared prefer/shadow path: returns (decision, provider) or (None, why)."""
    manifest = _active("routing")
    if manifest is None:
        return None, "no_manifest"
    declared = manifest.get("input") or {}
    contract = FEATURE_CONTRACTS.get(str(declared.get("identity") or ""))
    if contract is None:
        return None, "identity_mismatch"
    dimension, feature_fn = contract
    features = feature_fn(prompt)
    if declared.get("dimension") != dimension or len(features) != dimension:
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
    try:
        execution = _execution_provenance(response, manifest)
    except ValueError:
        return None, "invalid"
    scores = validated["scores"]
    winner = max(npu_contract.ROUTE_MODES, key=lambda mode: scores[mode])
    margin = abs(scores["autopilot"] - scores["workbench"])
    decision = {
        "scores": scores,
        "winner": winner,
        "margin": round(margin, 4),
        **execution,
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
        _record_fallback("routing", why)
        return None
    score = decision["scores"][decision["winner"]]
    if (
        decision["reason_code"] != "score_margin"
        or decision["margin"] < MIN_PREFER_MARGIN
        or score < MIN_PREFER_SCORE
    ):
        _record_fallback("routing", "low_confidence")
        return None
    _record_capability_event(
        "npu_route",
        "routing",
        mode=decision["winner"],
        provider=decision["provider"],
        accelerated=decision["accelerated"],
        ep_fallback=decision["ep_fallback"],
        simulated=decision["simulated"],
        margin=decision["margin"],
    )
    if decision["accelerated"]:
        source = "npu accelerator"
    elif decision["provider"] == "cpu":
        source = "cpu reference"
    elif decision["provider"] == "cpu-sim":
        source = "cpu simulator"
    else:
        source = "utility sidecar"
    return {
        "mode": decision["winner"],
        "tier": runtime_policy.route_tier(
            decision["winner"], policy, fallback="code",
        ),
        "reason": "%s (%s): score margin %.2f" % (
            source, decision["provider"], decision["margin"],
        ),
        "confidence": round(score, 4),
        "source": source,
    }


def route_shadow(prompt, baseline):
    """Shadow-mode comparison; never changes behavior, returns None always."""
    if _mode("routing") != "shadow":
        return None
    decision, why = _routing_call(prompt)
    if decision is None:
        with _STATE_LOCK:
            _SHADOW["errors"] += 1
        _ledger_record("error")
        _record_capability_event(
            "npu_route_shadow", "routing", ok=False, reason=why,
        )
        return None
    baseline_mode = str((baseline or {}).get("mode") or "")
    agree = baseline_mode == decision["winner"]
    with _STATE_LOCK:
        _SHADOW["agree" if agree else "disagree"] += 1
    _ledger_record("agree" if agree else "disagree")
    _record_capability_event(
        "npu_route_shadow",
        "routing",
        ok=True,
        agree=agree,
        npu_mode=decision["winner"],
        baseline_mode=baseline_mode,
        provider=decision["provider"],
        accelerated=decision["accelerated"],
        ep_fallback=decision["ep_fallback"],
        simulated=decision["simulated"],
        margin=decision["margin"],
    )
    return None


def _embedding_call(manifest, text):
    limit = int((manifest.get("limits") or {}).get("max_text_chars") or 0)
    prompt = str(text or "")
    if limit > 0 and len(prompt) > limit:
        raise ValueError("embedding text exceeds the manifest limit")
    broker = npu_broker.get_broker()
    response = broker.call(manifest, {"kind": "embedding", "texts": [prompt]})
    vectors = response.get("vectors")
    if not isinstance(vectors, list) or len(vectors) != 1:
        raise ValueError("embedding response must carry exactly one vector")
    vector = npu_contract.validate_vector(
        vectors[0], int(manifest.get("dimension") or 0),
    )
    execution = _execution_provenance(response, manifest)
    return {
        "vector": vector,
        **execution,
        "manifest_hash": str(manifest.get("manifest_hash") or ""),
    }


def embeddings_active() -> str:
    """Effective embeddings accelerator mode: off, shadow, or prefer."""
    return _mode("embeddings")


def embed_for_space(text, model_identity, revision, expected_dimension=None):
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
        _record_fallback("embeddings", "no_manifest")
        return None
    space = manifest.get("space")
    if not space:
        _record_fallback("embeddings", "space_unpinned")
        return None
    if (
        isinstance(expected_dimension, bool)
        or not isinstance(expected_dimension, int)
        or expected_dimension <= 0
        or manifest.get("dimension") != expected_dimension
        or space.get("dimension") != expected_dimension
    ):
        _record_fallback("embeddings", "dimension_mismatch")
        return None
    if (
        space.get("model") != str(model_identity or "")
        or space.get("revision") != str(revision or "")
    ):
        _record_fallback("embeddings", "space_mismatch")
        return None
    try:
        result = _embedding_call(manifest, text)
    except npu_broker.NpuUnavailable as exc:
        _record_fallback("embeddings", exc.reason)
        return None
    except (TypeError, ValueError):
        _record_fallback("embeddings", "invalid")
        return None
    except Exception:
        _record_fallback("embeddings", "internal")
        return None
    if result.get("simulated"):
        _record_fallback("embeddings", "simulated_provider")
        return None
    if result.get("provider") != "cpu" and not result.get("accelerated"):
        # VitisAI spans NPU and non-NPU target classes. Until the effective
        # device can be attested, it may participate in shadow/routing work but
        # cannot substitute a production embedding vector.
        _record_fallback("embeddings", "target_unattested")
        return None
    result.update(
        model=space["model"],
        revision=space["revision"],
        dimension=space["dimension"],
    )
    _record_capability_event(
        "npu_embed",
        "embeddings",
        provider=result["provider"],
        accelerated=result["accelerated"],
        ep_fallback=result["ep_fallback"],
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
        _record_capability_event(
            "npu_embed_shadow", "embeddings", ok=False, reason=exc.reason,
        )
        return None
    except Exception:
        _record_capability_event(
            "npu_embed_shadow", "embeddings", ok=False, reason="invalid",
        )
        return None
    _record_capability_event(
        "npu_embed_shadow",
        "embeddings",
        ok=True,
        provider=result["provider"],
        accelerated=result["accelerated"],
        ep_fallback=result["ep_fallback"],
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
        "error": npu_contract.sanitize_error(
            manifest.get("error") or "", 200,
        ),
    }
    if manifest.get("operation") == "embedding":
        summary["dimension"] = manifest.get("dimension")
        summary["space_pinned"] = bool(manifest.get("space"))
    else:
        summary["input_identity"] = str(
            (manifest.get("input") or {}).get("identity") or ""
        )
    return summary


def _fallback_status(modes, broker_status) -> dict:
    capabilities = {}
    for capability in ("routing", "embeddings"):
        policy_mode = str(modes.get(capability) or "").strip().lower()
        if policy_mode not in {"off", "shadow", "prefer"}:
            policy_mode = "unknown"
        capabilities[capability] = {
            "policy_mode": policy_mode,
            "role": (
                "observer" if policy_mode == "shadow"
                else "executor" if policy_mode == "prefer"
                else "disabled" if policy_mode == "off"
                else "unknown"
            ),
            "local_fallback_handler": (
                "ollama" if policy_mode in {"shadow", "prefer"} else "unknown"
            ),
        }
    reason_counts = {}
    for raw_reason, raw_count in (broker_status.get("fallbacks") or {}).items():
        reason = npu_contract.fallback_reason(raw_reason)
        try:
            count = min(2_147_483_647, max(0, int(raw_count or 0)))
        except (TypeError, ValueError):
            count = 0
        reason_counts[reason] = reason_counts.get(reason, 0) + count
    with _STATE_LOCK:
        latest = dict(_LAST_FALLBACK)
    known = bool(latest.get("known"))
    if not known and any(reason_counts.values()):
        observed = [reason for reason, count in reason_counts.items() if count]
        latest = {
            "known": True,
            "capability": "unknown",
            "reason": observed[0] if len(observed) == 1 else "unknown",
            "operation_mode": "unknown",
            "fallback_handler": "unknown",
            "handler_state": "observed",
            "count": sum(reason_counts.values()),
        }
        known = True
    return {
        "schema_version": 1,
        "known": known,
        "capabilities": capabilities,
        "last_fallback": {
            "capability": npu_contract.fallback_capability(
                latest.get("capability")
            ),
            "reason": npu_contract.fallback_reason(latest.get("reason")),
            "operation_mode": npu_contract.fallback_operation_mode(
                latest.get("operation_mode")
            ),
            "fallback_handler": npu_contract.fallback_handler(
                latest.get("fallback_handler")
            ),
            "handler_state": npu_contract.fallback_handler_state(
                latest.get("handler_state")
            ),
            "count": min(
                2_147_483_647, max(0, int(latest.get("count") or 0)),
            ),
        },
        "reason_counts": reason_counts,
    }


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
    broker_status = dict(broker.status())
    hello = dict(broker_status.get("hello") or {})
    hello["ort_error"] = npu_contract.sanitize_error(
        hello.get("ort_error") or "", 160,
    )
    broker_status["hello"] = hello
    provider_rows = []
    seen_provider_ids = set()
    for raw_row in broker_status.get("providers") or []:
        if not isinstance(raw_row, dict):
            continue
        provider_id = raw_row.get("id")
        if (
            not isinstance(provider_id, str)
            or provider_id not in npu_contract.PROVIDER_IDS
            or provider_id in seen_provider_ids
        ):
            continue
        seen_provider_ids.add(provider_id)
        provider_row = {
            "id": provider_id,
            "label": npu_providers.provider_label(provider_id),
            "registered": raw_row.get("registered") is True,
            "detected": False,
            "runtime_ready": raw_row.get("runtime_ready") is True,
            "ep": (
                npu_providers.SIMULATOR_EP
                if provider_id == "cpu-sim"
                else npu_providers.PROVIDER_EPS.get(provider_id, "")
            ),
            "reason": npu_contract.sanitize_error(
                raw_row.get("reason") or "", 160,
            ),
        }
        provider_rows.append(provider_row)
    broker_status["providers"] = provider_rows
    active_hashes = {
        str(manifest.get("manifest_hash") or "")
        for manifest in active.values()
        if manifest and manifest.get("manifest_hash")
    }
    model_rows = []
    for raw_row in broker_status.get("models") or []:
        if not isinstance(raw_row, dict):
            continue
        # Broker workers can outlive a manifest deletion/replacement until the
        # idle reaper runs. Only sessions for the current active manifests may
        # contribute to readiness (or appear as current model state).
        if raw_row.get("manifest_hash") not in active_hashes:
            continue
        model_row = dict(raw_row)
        model_row.pop("manifest_hash", None)
        model_row["error"] = npu_contract.sanitize_error(
            model_row.get("error") or "", 200,
        )
        model_rows.append(model_row)
    broker_status["models"] = model_rows
    broker_status["last_error"] = npu_contract.sanitize_error(
        broker_status.get("last_error") or "", 200,
    )
    providers = broker_status.get("providers") or []
    try:
        npu_vendor, _npu_name, detected_raw = (
            system_profile.detect_npu_hardware()
        )
        detected = bool(detected_raw)
    except Exception:
        npu_vendor = "unknown"
        detected = None
    provider_for_vendor = {
        "amd": "vitisai",
        "intel": "openvino",
        "qualcomm": "qnn",
    }.get(str(npu_vendor or "").lower(), "")
    for provider_row in providers:
        provider_row["detected"] = bool(
            detected is True and provider_row.get("id") == provider_for_vendor
        )
    model_rows = broker_status.get("models") or []
    ready_provider_ids = {
        row.get("provider")
        for row in model_rows
        if row.get("ok") is True and isinstance(row.get("provider"), str)
    }
    for provider_row in providers:
        provider_row["runtime_ready"] = (
            provider_row.get("id") in ready_provider_ids
        )
    invalid_only = bool(rows) and not any(
        not row.get("error") for row in rows
    )
    # A valid but cold manifest has not been probed yet, so readiness remains
    # unknown. An invalid-only configuration is conclusively not ready.
    readiness_observed = bool(providers or model_rows or invalid_only)
    utility_ready = (
        any(row.get("ok") is True for row in model_rows)
        if readiness_observed else None
    )
    runtime_ready = (
        any(row.get("ok") is True and row.get("npu_attested") is True
            for row in model_rows)
        if readiness_observed else None
    )
    circuit = broker_status.get("circuit") or {}
    healthy = (
        circuit.get("state") == "closed"
        and not circuit.get("consecutive_failures")
    )
    with _STATE_LOCK:
        shadow = dict(_SHADOW)
    fallback_status = _fallback_status(modes, broker_status)
    return {
        "modes": modes,
        "enabled": enabled,
        "detected": detected,
        "utility_ready": utility_ready,
        "runtime_ready": runtime_ready,
        "healthy": healthy,
        "features_id": FEATURES_ID,
        "feature_contracts": sorted(FEATURE_CONTRACTS),
        "shadow_live": shadow_advisory(),
        "manifest_count": len(rows),
        "manifest_errors": sum(1 for row in rows if row.get("error")),
        "manifests": {
            "routing": _manifest_summary(active["routing"]),
            "embedding": _manifest_summary(active["embedding"]),
        },
        "shadow": shadow,
        "broker": broker_status,
        "fallback_projection": fallback_status,
    }


def fallback_projection(state=None) -> dict:
    """Public enum-only fallback/capability state for status consumers."""
    state = status() if state is None else state
    projection = (
        state.get("fallback_projection") if isinstance(state, dict) else None
    )
    if not isinstance(projection, dict):
        return {
            "schema_version": 1,
            "known": False,
            "capabilities": {},
            "last_fallback": {},
            "reason_counts": {},
        }
    return json.loads(json.dumps(projection))


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
    line = (
        "routing=%s embeddings=%s detected=%s utility-ready=%s "
        "npu-runtime-ready=%s healthy=%s "
        "worker=%s circuit=%s p95=%sms"
        % (
            modes.get("routing"),
            modes.get("embeddings"),
            _flag(state["detected"]),
            _flag(state["utility_ready"]),
            _flag(state["runtime_ready"]),
            _flag(state["healthy"]),
            (broker_state.get("worker") or {}).get("state", "cold"),
            (broker_state.get("circuit") or {}).get("state", "closed"),
            (broker_state.get("latency_ms") or {}).get("p95", 0),
        )
    )
    fallback = (state.get("fallback_projection") or {}).get("last_fallback") or {}
    if (state.get("fallback_projection") or {}).get("known"):
        line += " fallback=%s/%s->%s:%s" % (
            fallback.get("capability", "unknown"),
            fallback.get("reason", "unknown"),
            fallback.get("fallback_handler", "unknown"),
            fallback.get("handler_state", "unknown"),
        )
    return line


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
        "  state: detected=%s utility-ready=%s npu-runtime-ready=%s "
        "enabled=%s healthy=%s" % (
            _flag(state["detected"]), _flag(state["utility_ready"]),
            _flag(state["runtime_ready"]),
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
        "  shadow-live: %s over %s decision(s) — %s" % (
            (
                "%.0f%%" % (100.0 * state["shadow_live"]["agreement"])
                if state["shadow_live"].get("agreement") is not None
                else "n/a"
            ),
            state["shadow_live"].get("window", 0),
            state["shadow_live"].get("advisory", ""),
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
    projection = state.get("fallback_projection") or {}
    # Only render the controlled projection. Broker error/counter keys are an
    # internal diagnostic surface and may contain arbitrary provider text.
    fallbacks = projection.get("reason_counts") or {}
    if fallbacks:
        rendered = ", ".join(
            "%s=%s" % (key, fallbacks[key]) for key in sorted(fallbacks)
        )
        lines.append("  fallbacks: %s" % rendered)
        # A bare "ram_gate=357" reads as harmless bookkeeping. Each one is an
        # embedding that went to the model server instead, and when the server
        # is capped at one loaded model that evicts the generation model and
        # reloads it afterwards - measured at ~7.5s per swap on the development host. Say
        # so, and name the knob, so the count is legible as a cost.
        gated = int(fallbacks.get("ram_gate") or 0)
        if gated and gated >= max(
            int(count) for count in fallbacks.values()
        ):
            lines.append(
                "  note: %d call(s) fell back for free RAM, each one an "
                "embedding served by the model server instead; with a "
                "one-model cap that evicts and reloads the generation model. "
                "Tune SONDER_NPU_MIN_FREE_RAM_GB (default 2) or free RAM to "
                "let the accelerator take them." % gated
            )
    last_fallback = projection.get("last_fallback") or {}
    lines.append(
        "  fallback projection: %s"
        % (
            "%s/%s mode=%s handler=%s state=%s"
            % (
                last_fallback.get("capability", "unknown"),
                last_fallback.get("reason", "unknown"),
                last_fallback.get("operation_mode", "unknown"),
                last_fallback.get("fallback_handler", "unknown"),
                last_fallback.get("handler_state", "unknown"),
            )
            if projection.get("known") else "none observed"
        )
    )
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
