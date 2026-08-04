"""Lesson aging + contradiction detection. Pure logic, stdlib only.

Lessons accumulate in the memory store, but their usefulness is not
constant over time: a lesson learned six months ago about a since-deleted
API is worse than a fresh one even if both were "good" when recorded. This
module provides the ranking math (exponential age decay blended with usage
evidence) and a contradiction detector that flags pairs of lessons which say
semantically similar things while carrying *opposite* outcome signals.

Everything here is a pure function: no database, no I/O, no clock, no env
read at import time, and no network. Similarity is supplied by an injected
``similarity_fn(a_text, b_text) -> float`` so callers wire in real embeddings
while tests stay offline and deterministic. Wiring into memory_store /
retriever is done elsewhere; this file is logic only.
"""
import math

# Default half-life for age decay, in days. After this many days a lesson's
# age-decayed contribution is halved; after two half-lives it is quartered,
# and so on. Thirty days is a deliberately gentle default -- lessons stay
# relevant for weeks but a stale one steadily loses ground to fresher ones.
DEFAULT_HALF_LIFE_DAYS = 30.0

# Weight applied to per-lesson usage evidence when forming an effective score.
# Kept small so proven-useful lessons get a nudge above equally-scored peers
# without letting raw usage volume swamp the base reward signal.
DEFAULT_USAGE_WEIGHT = 0.05

# Similarity floor (inclusive) above which two lessons are considered to be
# "about the same thing" for contradiction detection. Below this we assume
# they simply address different topics and cannot contradict each other.
DEFAULT_SIM_THRESHOLD = 0.8

# Text tokens that flip the polarity of a directive-style lesson. Used only as
# a fallback when a lesson carries no explicit numeric/signal outcome.
_NEGATION_TOKENS = frozenset(
    {"not", "never", "no", "dont", "don't", "avoid", "without", "wont", "won't", "stop"}
)


def _finite(value, default=0.0):
    """Coerce ``value`` to a finite float, falling back to ``default``.

    NaN and +/-inf are treated as missing so a single malformed record can
    never poison a ranking with a non-comparable score.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(out):
        return float(default)
    return out


def decayed_score(base_score, age_days, *, half_life_days=DEFAULT_HALF_LIFE_DAYS):
    """Apply exponential age decay to ``base_score``.

    Returns ``base_score * 0.5 ** (age_days / half_life_days)``: at age 0 the
    score is unchanged, at one half-life it is halved, at two it is quartered.
    Decay is monotonic in age for a positive base score (older -> lower) and
    monotonic upward for a negative base score (a bad lesson's penalty also
    fades with age, which is intended -- old mistakes stop dominating).

    Negative ages are clamped to 0 (a lesson cannot be fresher than "now").
    A non-positive ``half_life_days`` is meaningless, so decay is skipped and
    the base score is returned unchanged.
    """
    base = _finite(base_score)
    age = max(0.0, _finite(age_days))
    hl = _finite(half_life_days)
    if hl <= 0.0:
        return base
    return base * (0.5 ** (age / hl))


def usage_credit(uses, hits, *, usage_weight=DEFAULT_USAGE_WEIGHT):
    """Scalar credit rewarding proven-useful lessons and penalizing harmful ones.

    ``uses`` is how many times a lesson was surfaced/applied; ``hits`` is how
    many of those uses led to a good outcome. Credit is
    ``usage_weight * (hits - misses)`` where ``misses = max(uses - hits, 0)``:
    a lesson that helped more often than it hurt earns positive credit, one
    that hurt more earns negative credit, and an unused lesson earns zero.
    """
    u = max(0, int(_finite(uses)))
    h = int(_finite(hits))
    # Hits cannot exceed uses or drop below zero; clamp defensively.
    h = max(0, min(h, u))
    misses = u - h
    return _finite(usage_weight) * (h - misses)


def effective_score(
    base_score,
    age_days,
    uses=0,
    hits=0,
    *,
    half_life_days=DEFAULT_HALF_LIFE_DAYS,
    usage_weight=DEFAULT_USAGE_WEIGHT,
):
    """Blend base reward, usage evidence, and age decay into one rank key.

    The base reward is aged via :func:`decayed_score`, then the usage credit
    from :func:`usage_credit` is added. Two lessons with the same base score
    and age order by usage; two with the same base and usage order by
    freshness. Higher is better.
    """
    aged = decayed_score(base_score, age_days, half_life_days=half_life_days)
    return aged + usage_credit(uses, hits, usage_weight=usage_weight)


def _lesson_field(lesson, name, default=None):
    """Read ``name`` from a dict-like or attribute-bearing lesson."""
    if isinstance(lesson, dict):
        return lesson.get(name, default)
    return getattr(lesson, name, default)


def rank_lessons(
    lessons,
    *,
    now_days,
    half_life_days=DEFAULT_HALF_LIFE_DAYS,
    usage_weight=DEFAULT_USAGE_WEIGHT,
):
    """Return ``lessons`` sorted by effective score, best first. Pure.

    Each lesson is a small dict (or attribute-bearing object) carrying
    ``score``, ``created_days``, ``uses`` and ``hits``. Age is derived as
    ``now_days - created_days``. The input list is not mutated -- a new list of
    the same lesson objects is returned. Ties break by newer ``created_days``
    then by original position, so ordering is fully deterministic.
    """
    now = _finite(now_days)
    indexed = list(enumerate(lessons))

    def sort_key(pair):
        idx, lesson = pair
        created = _finite(_lesson_field(lesson, "created_days", 0))
        age = now - created
        eff = effective_score(
            _lesson_field(lesson, "score", 0.0),
            age,
            _lesson_field(lesson, "uses", 0),
            _lesson_field(lesson, "hits", 0),
            half_life_days=half_life_days,
            usage_weight=usage_weight,
        )
        # Negate for descending effective score; newer (larger created) first
        # on ties; original index last as a stable final tie-break.
        return (-eff, -created, idx)

    indexed.sort(key=sort_key)
    return [lesson for _idx, lesson in indexed]


def _numeric_polarity(lesson):
    """Polarity (-1/0/+1) from an explicit numeric outcome, or None."""
    for name in ("score", "reward"):
        raw = _lesson_field(lesson, name, None)
        if raw is None:
            continue
        value = _finite(raw, default=0.0)
        if value > 0.0:
            return 1
        if value < 0.0:
            return -1
        return 0
    return None


def _signal_polarity(lesson):
    """Polarity from a coarse outcome ``signal`` string, or None if absent."""
    raw = _lesson_field(lesson, "signal", None)
    if raw is None:
        return None
    signal = str(raw).strip().lower()
    positive = {"used", "copied", "edited", "accepted", "compiled", "tests_passed", "good"}
    negative = {"rejected", "failed", "bad"}
    if signal in positive:
        return 1
    if signal in negative:
        return -1
    return 0


def _directive_polarity(text):
    """Fallback polarity from directive text: negated phrasing -> -1 else +1.

    This only ever runs when a lesson carries no numeric or signal outcome. It
    is intentionally crude -- a single negation token flips an otherwise
    affirmative directive -- and exists so two similar directives like
    "always retry on timeout" and "never retry on timeout" are recognized as
    opposites.
    """
    tokens = "".join(c if (c.isalnum() or c == "'") else " " for c in str(text).lower()).split()
    return -1 if any(tok in _NEGATION_TOKENS for tok in tokens) else 1


def _outcome_polarity(lesson):
    """Combined outcome polarity: numeric, then signal, then directive text."""
    for resolver in (_numeric_polarity, _signal_polarity):
        polarity = resolver(lesson)
        if polarity is not None:
            return polarity
    return _directive_polarity(_lesson_field(lesson, "text", ""))


def detect_contradictions(
    lessons,
    similarity_fn,
    *,
    sim_threshold=DEFAULT_SIM_THRESHOLD,
):
    """Flag pairs of lessons that are similar yet carry opposite outcomes.

    ``similarity_fn(a_text, b_text) -> float`` is injected so the caller
    supplies real embedding similarity while tests stay offline. For every
    unordered pair of lessons whose similarity is ``>= sim_threshold`` and
    whose outcome polarities are strictly opposite (one +1, one -1), a
    conflict record is emitted. Similar-but-agreeing pairs, dissimilar pairs,
    and pairs where either side is polarity-neutral are never flagged.

    Outcome polarity is resolved from a lesson's numeric ``score``/``reward``,
    else a coarse ``signal`` string, else the directive polarity of its
    ``text``. Returns a list of dicts ``{"a", "b", "similarity", "reason"}``
    in deterministic (i < j) order. Pure -- inputs are not mutated.
    """
    items = list(lessons)
    threshold = _finite(sim_threshold)
    conflicts = []
    for i in range(len(items)):
        a = items[i]
        a_text = _lesson_field(a, "text", "")
        a_pol = _outcome_polarity(a)
        for j in range(i + 1, len(items)):
            b = items[j]
            if a_pol == 0:
                continue
            b_pol = _outcome_polarity(b)
            if b_pol == 0 or a_pol == b_pol:
                continue
            sim = _finite(similarity_fn(a_text, _lesson_field(b, "text", "")), default=0.0)
            if sim < threshold:
                continue
            reason = (
                "similar (sim={:.3f} >= {:.3f}) but opposite outcomes "
                "({:+d} vs {:+d})".format(sim, threshold, a_pol, b_pol)
            )
            conflicts.append({"a": a, "b": b, "similarity": sim, "reason": reason})
    return conflicts
