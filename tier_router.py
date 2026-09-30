"""Route a request to the tier measured best for its KIND of work.

The one durable finding from this project's model measurements: a local
7B-class model is strong when every fact it needs is in the prompt
(TRANSFORMATION -- restructure this, mirror this struct, implement this
specified function) and weak when it must supply a fact from memory (RECALL --
an API signature, a lookup table, a standard's exact wording). A lookup table
looks mechanical and is the worst case, because it is pure recall.

So the routing rule is not "hard vs easy" -- it is "are the facts in the prompt
or not". This classifier reads that signal from the request text and returns a
tier suggestion, with the reason, so the choice is legible rather than magic.

The primary classifier is deliberately lexical: routing must be cheap and
must not itself depend on the model whose weakness it is compensating for.
Transformation, recall, and reasoning always win. Only the otherwise-general
case can opt into a local embedding backstop with SONDER_SEMANTIC_TIER_ROUTING=1
or [features] semantic_tier_routing=true. Default OFF; no generation model or
remote endpoint is used for this signal. Vision requires an image and is never
selected by this text-only router.

The signal is a trained linear head over nomic-embed-text (logistic regression
on the bake-off bank plus locally generated synthetic prompts; no user data),
shipped as sonder_runtime/adapters/data/semantic_tier_head.json;
SONDER_SEMANTIC_TIER_HEAD points at an operator's own head instead. Measured
2026-09-28 live against local Ollama on the 96 text prompts of the held-out
split: this lexical-first hybrid 81.2% (lexical alone 29.2%); the head decided
70, 66 correctly; 47 ms median. It abstains unless the top probability beats
the runner-up by 0.3 (fixed before measuring). Without a head matching the
embedding model, the adapter falls back to centroids over the bank (84.2% as a
pure classifier in the bake-off; its 0.05 cosine gate is uncalibrated).
Cold centroids warm lazily; any failure or one-second deadline yields the
original lexical fallback. The returned signal and reason expose the choice.
"""
from __future__ import annotations

import math
import os
import re

from sonder_runtime.domain.routing.semantic_tier import MIN_MARGIN
from sonder_runtime.platform.config_environment import env_bool

# Verbs whose object is usually PRESENT in the prompt -- you transform text you
# were given. Strong local-model territory.
_TRANSFORM = re.compile(
    r"\b(refactor|restructure|rewrite|convert|translate|port|rename|reformat|"
    r"inline|extract|simplify|implement|mirror|transcribe|reorganiz|split|"
    r"merge|deduplicate|format|indent|annotate|type[- ]?hint|remove|delete|"
    r"clean\s?up|fix\s+this|add\s+a\s+(guard|check|test))\w*", re.I)

# Verbs/nouns that demand a fact the model must REMEMBER. Weak local-model
# territory; prefer a stronger/cloud tier.
_RECALL = re.compile(
    r"\b(what\s+is|what'?s|which|who|when|where|recall|remember|exact|"
    r"signature|api|parameters?\s+of|arguments?\s+of|default\s+value|"
    r"truth\s+table|lookup\s+table|standard|rfc\b|spec(ification)?|"
    r"version\s+of|list\s+all|enumerate|history\s+of|difference\s+between)\w*",
    re.I)

# Cues that the answer needs multi-step reasoning rather than a fact or a
# transform -- worth a reasoning tier if one is configured.
_REASON = re.compile(
    r"\b(why\s+(does|is|would|did)|prove|derive|explain\s+why|trade[- ]?off|"
    r"design\s+a|architect|reason\s+through|step\s+by\s+step|analy[sz]e\s+the)\w*",
    re.I)

# A fenced block, a pasted signature, or an explicit "here is X" means the
# material is IN the prompt -- pushes toward transformation regardless of verb.
_MATERIAL_PRESENT = re.compile(
    r"```|\bdef\s+\w+\s*\(|\bclass\s+\w+|\bhere\s+is\b|\bthis\s+(code|function|"
    r"file|struct|enum|snippet)\b|\bfollowing\s+(code|function)\b", re.I)


def classify(prompt: str) -> str:
    """One of "transformation", "recall", "reasoning", or "general"."""
    text = prompt or ""
    transform = bool(_TRANSFORM.search(text))
    recall = bool(_RECALL.search(text))
    reason = bool(_REASON.search(text))
    material = bool(_MATERIAL_PRESENT.search(text))

    # Material pasted into the prompt is the strongest signal that the facts are
    # present -- you usually work ON the code you paste. This dominates a recall
    # verb ("which branch is dead") that refers to the pasted material. The
    # residual edge case (paste code AND ask about an EXTERNAL api) is rare and
    # the router is only a suggestion the caller can override.
    if material:
        return "transformation"
    if recall and not transform:
        return "recall"
    if reason and not transform:
        return "reasoning"
    if transform:
        return "transformation"
    if recall:
        return "recall"
    if reason:
        return "reasoning"
    return "general"


# Which tier each kind should prefer, and why. Cloud tiers are only chosen for
# the kinds the local model is measured worst at; a marginal cloud edge on
# real delegated work (~3pp) does not justify routing transformation off-box.
_PREFERENCE = {
    "transformation": ("code", "facts are in the prompt -- the local model's strong axis"),
    "recall": ("cloud-general",
               "needs a remembered fact; the local model was measured wrong 3/3 on recall"),
    "reasoning": ("reasoning", "multi-step reasoning; use the reasoning tier"),
    "general": ("code", "no strong signal either way; the default local tier"),
}


def _semantic_signal(prompt, classifier, embedder):
    """Lazy adapter boundary; injected callbacks keep routing tests offline."""
    try:
        if classifier is None:
            from sonder_runtime.adapters.semantic_tier import semantic_signal

            result = semantic_signal(prompt, embedder=embedder)
        else:
            result = classifier(prompt)
        if not isinstance(result, dict):
            return None
        margin = result.get("margin")
        if (result.get("tier") not in {"fast", "general", "code", "reasoning"}
                or isinstance(margin, bool) or not isinstance(margin, (int, float))
                or not math.isfinite(margin) or not MIN_MARGIN <= margin <= 2
                or not isinstance(result.get("model"), str) or not result["model"]):
            return None
        return result
    except Exception:
        return None


def route(prompt: str, available_tiers=None, *, semantic_enabled=None,
          semantic_classifier=None, embedder=None, recent_evidence=None,
          identity_for=None, tier_models=None, tools=False,
          structured_output=False, has_image=False, approx_tokens=0,
          long_context=False, required_capabilities=None,
          request_payload=None, capability_routing=None) -> dict:
    """Suggest a tier for `prompt`.

    Returns {"kind", "tier", "reason", "fallback_used", "signal"}. If the preferred tier
    is not among `available_tiers`, falls back to a present one and says so --
    a router that names an unconfigured tier would just fail the next call.
    Optional keyword injections do not change existing positional callers.
    An unavailable semantic winner abstains rather than promoting a runner-up.
    With no text-capable tier configured, retain the historical "code" sentinel.
    """
    kind = classify(prompt)
    tier, reason = _PREFERENCE[kind]
    signal = "lexical"
    from sonder_runtime.adapters.inference.capability_evidence import (
        capability_routing_mode,
    )
    mode = capability_routing_mode(
        None if capability_routing is None else {"SONDER_CAPABILITY_ROUTING": capability_routing}
    )
    evidence_notes = []
    evidence_fallback = False
    evidence_required = frozenset(required_capabilities or ())
    requirements_error = False
    if recent_evidence is not None:
        try:
            from sonder_runtime.application.routing.request_capabilities import (
                request_requirements,
            )
            evidence_required |= request_requirements(
                request_payload, prompt=prompt, tools=tools,
                structured_output=structured_output, has_image=has_image,
                approx_tokens=approx_tokens, long_context=long_context,
            )
        except (ImportError, TypeError, ValueError):
            requirements_error = bool(
                evidence_required or tools or structured_output or has_image
                or long_context or approx_tokens or request_payload is not None
            )
    if available_tiers is not None:
        # Keep the caller's order: the last-resort fallback takes the first entry.
        available_tiers = [tier_name for tier_name in available_tiers
                           if tier_name != "vision" or "vision" in evidence_required]
    if requirements_error and mode == "strict":
        return {"kind": kind, "tier": None, "reason": "request capability requirements are invalid",
                "fallback_used": True, "signal": signal}
    if recent_evidence is not None and mode != "off" and (evidence_required or requirements_error):
        tier_models = tier_models or {}
        eligible_tiers = []
        refusals = []
        for candidate in available_tiers if available_tiers is not None else tuple(tier_models):
            model = tier_models.get(candidate)
            try:
                from sonder_runtime.application.routing.request_capabilities import (
                    check_request_evidence,
                )
                identity = identity_for(model) if identity_for is not None and model else None
                allowed, evidence_reason = check_request_evidence(
                    recent_evidence, model, evidence_required,
                    backend="ollama", identity=identity, mode=mode,
                )
            except (AttributeError, ImportError, TypeError, ValueError, OSError, RuntimeError):
                allowed = mode == "advisory"
                evidence_reason = "unverified: recent capability evidence unavailable"
            if allowed:
                eligible_tiers.append(candidate)
                evidence_notes.append(f"{candidate}: {evidence_reason}")
            else:
                refusals.append(f"{candidate}: {evidence_reason}")
        if not eligible_tiers and mode == "strict":
            return {"kind": kind, "tier": None,
                    "reason": "no eligible tier for required capabilities (%s)" %
                    ("; ".join(refusals) or "recent capability evidence unavailable"),
                    "fallback_used": True, "signal": signal}
        if eligible_tiers:
            available_tiers = eligible_tiers
        elif refusals:
            evidence_fallback = True
            evidence_notes.append("capability fallback_used: all candidates failed; retaining configured routing")
        else:
            evidence_notes.append("unverified: model identity unavailable")
        if refusals:
            evidence_notes.append("capability evidence refused: " + "; ".join(refusals))
    enabled = (env_bool(os.environ.get("SONDER_SEMANTIC_TIER_ROUTING", "0"))
               if semantic_enabled is None else semantic_enabled is True)
    if kind == "general" and enabled:
        semantic = _semantic_signal(prompt, semantic_classifier, embedder)
        if semantic and (available_tiers is None or semantic["tier"] in available_tiers):
            tier = semantic["tier"]
            reason = (f"semantic tier={tier}; margin={semantic['margin']:.3f}; "
                      f"embedding model={semantic['model']}")
            signal = "semantic"
    fallback_used = evidence_fallback
    if available_tiers is not None and tier not in available_tiers:
        fallback_used = True
        for candidate in ("code", "general", "reasoning"):
            if candidate in available_tiers:
                tier = candidate
                break
        else:
            tier = next(iter(available_tiers), "code")
        reason += " (preferred tier unavailable; using %s)" % tier
    if evidence_notes:
        reason += " (" + "; ".join(evidence_notes) + ")"
    return {"kind": kind, "tier": tier, "reason": reason,
            "fallback_used": fallback_used, "signal": signal}
