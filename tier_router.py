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

Deliberately a lexical classifier, not a model call: routing must be cheap and
must not itself depend on the model whose weakness it is compensating for.
"""
from __future__ import annotations

import re

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


def route(prompt: str, available_tiers=None) -> dict:
    """Suggest a tier for `prompt`.

    Returns {"kind", "tier", "reason", "fallback_used"}. If the preferred tier
    is not among `available_tiers`, falls back to a present one and says so --
    a router that names an unconfigured tier would just fail the next call.
    """
    kind = classify(prompt)
    tier, reason = _PREFERENCE[kind]
    fallback_used = False
    if available_tiers is not None and tier not in available_tiers:
        fallback_used = True
        for candidate in ("code", "general", "reasoning"):
            if candidate in available_tiers:
                tier = candidate
                break
        else:
            tier = next(iter(available_tiers), "code")
        reason += " (preferred tier unavailable; using %s)" % tier
    return {"kind": kind, "tier": tier, "reason": reason,
            "fallback_used": fallback_used}
