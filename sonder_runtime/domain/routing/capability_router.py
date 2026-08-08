"""Capability-aware routing: classify a request, pick the best tier, escalate.

This is the "which brain for this job" decision, as **pure rules** — no I/O, no
environment, no model calls. It takes the request text plus a few cheap signals
and returns a :class:`Route`: the task class, the recommended tier, an ordered
escalation ladder, and whether the task wants a web/current-information tool.

Design notes:
- Tiers are *names* the runtime policy binds to concrete models
  (``fast``/``code``/``general`` today; ``reasoning``/``vision``/``oracle`` when
  the operator adds them). Routing degrades gracefully: if a preferred tier is
  not in ``available`` it falls back down a per-class chain to something that is.
- The ``oracle`` tier (a big local model or a consented cloud model) is only
  ever offered when ``allow_oracle`` is true — the escalation path cannot leak a
  request off-box without explicit consent, mirroring the rest of Sonder.
- Classification is deliberately simple and transparent (keyword/-signal
  scoring). It is a strong starting heuristic, not a learned model; the live
  router may refine it, but correctness never depends on it being right — a
  mis-route only picks a differently-capable local model.
"""
from __future__ import annotations

from dataclasses import dataclass, field

TASK_CLASSES = ("simple", "search", "code", "reasoning", "vision", "long_context")

# Escalation reasons the caller may report to step up a tier.
ESCALATION_REASONS = ("failed", "low_confidence", "empty_response", "explicit_hard")

# Preferred tier per task class (strongest-fit first choice).
_PREFERRED = {
    "simple": "fast",
    "search": "fast",
    "code": "code",
    "reasoning": "reasoning",
    "vision": "vision",
    "long_context": "general",
}

# Ascending-strength ladder per class. Filtered to available tiers at runtime;
# ``oracle`` is appended only when the caller allows it.
_LADDER = {
    "simple": ("fast", "general", "reasoning"),
    "search": ("fast", "general"),
    "code": ("code", "general", "reasoning"),
    "reasoning": ("reasoning", "general", "code"),
    "vision": ("vision", "general"),
    "long_context": ("general", "reasoning"),
}

# Fallback order when a preferred/ladder tier is not configured. Everything can
# fall back to one of these, which are the always-present base tiers.
_FALLBACK = ("general", "code", "fast")

_LONG_CONTEXT_TOKENS = 6000  # a request bigger than this is a long-context job

_KEYWORDS = {
    "code": (
        "code", "function", "implement", "bug", "refactor", "compile", "traceback",
        "exception", "stack trace", "unit test", "pytest", "regex", "algorithm",
        "python", "javascript", "typescript", "rust", "golang", " java ", "c++",
        "sql", "api", "class ", "def ", "async", "docker", "kubernetes",
    ),
    "reasoning": (
        "prove", "proof", "theorem", "derive", "step by step", "reason through",
        "think carefully", "think hard", "plan the", "trade-off", "tradeoff",
        "analyze", "why does", "why is", "explain the reasoning", "complexity",
        "optimi", "strategy", "design a system", "architecture for",
    ),
    "search": (
        "latest", "current", "today", "right now", "this week", "news",
        "look up", "search for", "who won", "weather", "price of", "release date",
        "recent", "up to date", "what happened",
    ),
    "vision": (
        "image", "screenshot", "photo", "picture", "diagram", "this chart",
        "what's in this", "describe the", "ocr", "read the text in",
    ),
    "long_context": (
        "summarize this", "summarise this", "this document", "the whole repo",
        "this codebase", "these files", "the attached", "this transcript",
        "tl;dr of", "condense",
    ),
}

_HARD_CUES = ("think hard", "think carefully", "step by step", "this is hard",
              "be rigorous", "reason through")


@dataclass(frozen=True)
class Route:
    task: str
    tier: str
    ladder: tuple[str, ...]
    confidence: float
    want_web: bool = False
    signals: dict = field(default_factory=dict)


def _score(text: str) -> dict:
    low = " " + (text or "").lower() + " "
    return {cls: sum(1 for kw in kws if kw in low) for cls, kws in _KEYWORDS.items()}


def classify_task(
    prompt: str,
    *,
    has_image: bool = False,
    approx_tokens: int = 0,
    explicit: str | None = None,
) -> tuple[str, float]:
    """Return ``(task_class, confidence)`` for a request.

    ``explicit`` (one of :data:`TASK_CLASSES`) is an operator/tool override and
    always wins. ``has_image`` forces vision; a very large ``approx_tokens``
    forces long-context. Otherwise the highest keyword score wins, defaulting to
    ``simple`` for short, unmarked requests.
    """
    if explicit in TASK_CLASSES:
        return explicit, 1.0
    if has_image:
        return "vision", 1.0
    scores = _score(prompt)
    # Size dominates: a genuinely large payload is a long-context job regardless
    # of surface keywords.
    if approx_tokens >= _LONG_CONTEXT_TOKENS:
        return "long_context", 0.9
    best = max(scores, key=lambda c: scores[c])
    top = scores[best]
    if top == 0:
        return "simple", 0.4
    total = sum(scores.values()) or 1
    # Confidence: share of hits going to the winner, floored so a lone strong
    # cue still reads as reasonably confident.
    confidence = max(0.5, min(0.95, top / total))
    return best, round(confidence, 2)


def _resolve(tier: str, available) -> str:
    if tier in available:
        return tier
    for fb in _FALLBACK:
        if fb in available:
            return fb
    # Nothing matched (misconfigured policy) — return the requested tier as-is
    # so the caller surfaces a clear "unknown tier" error rather than silence.
    return tier


def recommend_tier(task: str, available) -> str:
    """Best available tier for a task class, degrading to a base tier."""
    available = set(available or ())
    return _resolve(_PREFERRED.get(task, "general"), available)


def escalation_ladder(task: str, available, *, allow_oracle: bool = False) -> tuple[str, ...]:
    """Ordered, de-duplicated ladder of available tiers to try for a task.

    Strongest-fit first, then progressively more capable base tiers, then the
    ``oracle`` tier only if ``allow_oracle`` — a big local model or a consented
    cloud model, the last resort for the genuinely hard minority.
    """
    available = set(available or ())
    raw = list(_LADDER.get(task, ("general", "code")))
    if allow_oracle:
        raw.append("oracle")
    out: list[str] = []
    for tier in raw:
        resolved = tier if tier in available or tier == "oracle" else None
        if resolved and resolved not in out:
            out.append(resolved)
    if not out:  # ensure a non-empty ladder even on a sparse policy
        out.append(recommend_tier(task, available))
    return tuple(out)


def next_tier(ladder, current: str, reason: str) -> str | None:
    """Step to the next tier in a ladder, or ``None`` if exhausted.

    Only the recognized escalation reasons step up; anything else stays put
    (returns ``None``) so a satisfied result never wastes a stronger model.
    """
    if reason not in ESCALATION_REASONS:
        return None
    ladder = tuple(ladder or ())
    if current not in ladder:
        return ladder[0] if ladder else None
    idx = ladder.index(current)
    return ladder[idx + 1] if idx + 1 < len(ladder) else None


def route(
    prompt: str,
    available,
    *,
    has_image: bool = False,
    approx_tokens: int = 0,
    explicit: str | None = None,
    allow_oracle: bool = False,
) -> Route:
    """One-call capability route: classify, pick a tier, build the ladder."""
    task, confidence = classify_task(
        prompt, has_image=has_image, approx_tokens=approx_tokens, explicit=explicit
    )
    tier = recommend_tier(task, available)
    ladder = escalation_ladder(task, available, allow_oracle=allow_oracle)
    # A low-confidence classification starts one rung down the ladder's stronger
    # end is not automatic; we keep the recommended tier but flag it so the live
    # router can choose to pre-escalate.
    want_web = task == "search"
    return Route(
        task=task,
        tier=tier,
        ladder=ladder,
        confidence=confidence,
        want_web=want_web,
        signals={"has_image": has_image, "approx_tokens": approx_tokens},
    )
