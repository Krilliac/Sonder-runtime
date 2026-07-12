"""difficulty_estimator — heuristic task-difficulty score in [0, 1].

sonder's solver spends a FIXED repair budget (max_attempts) on every task,
whether it's "reverse a string" or "write a thread-safe LRU cache with a
custom eviction policy". That's wasted budget on the easy end and often not
enough on the hard end. This module scores a task's TEXT (no execution, no
model call) so callers can route effort BEFORE spending a single generation:
more solver.solve() attempts / best_of_n candidates for hard tasks, fewer for
easy ones — trading token budget in proportion to expected difficulty instead
of a flat guess.

Three deterministic signals combine into the score:
  - length      — longer specs tend to carry more hidden complexity
                  (saturating, so a 5000-word essay doesn't dominate)
  - keywords    — surface signals correlated with genuinely harder code:
                  recursion, dynamic programming, concurrency, parsing, regex
  - constraints — imperative/numeric requirements ("must", "at most N",
                  numbered lists, complexity bounds) — more constraints to
                  satisfy simultaneously means more ways to get it wrong

Pure and stdlib-only: one function of a string in, one float out (plus an
optional breakdown for logging/debugging). No GPU, no network, no subprocess.
"""
import math
import re

# --- keyword signals ---------------------------------------------------------
# Each category is a compiled pattern; a category fires at most once (repeating
# "recursive... recursive... recursive" isn't 3x harder) so the score reflects
# which HARD TOPICS are present, not how many times they're mentioned.
KEYWORD_CATEGORIES = {
    "recursion": re.compile(
        r"\brecursi(on|ve(ly)?)\b|\bbase case\b|\bcall(s|ing)? itself\b", re.I),
    "dynamic_programming": re.compile(
        r"\bdynamic programming\b|\bmemoi[sz](e|ation)\b|\btabulation\b|\bDP\b|"
        r"\boptimal sub(structure|problem)", re.I),
    "concurrency": re.compile(
        r"\bthread(s|ing)?\b|\basync(io)?\b|\bconcurren(t|cy)\b|\bmutex\b|"
        r"\block(s|ing)?\b|\brace condition\b|\bdeadlock\b|\bsemaphore\b|"
        r"\bmulti\s?process(ing)?\b|\batomic(ally)?\b", re.I),
    "parsing": re.compile(
        r"\bpars(e|er|ing)\b|\bgrammar\b|\btoken(s|ize|izer)?\b|\bAST\b|"
        r"\bsyntax tree\b|\blexer\b|\bcompiler\b", re.I),
    "regex": re.compile(
        r"\bregex\b|\bregular expression\b|\bre\.(match|search|sub|findall|compile)\b", re.I),
    # bonus categories beyond the required five — same "fires once" discipline,
    # kept low-weight so they nudge rather than dominate.
    "graph": re.compile(
        r"\bgraph\b|\bBFS\b|\bDFS\b|\btopological\b|\bshortest path\b|\bdijkstra\b", re.I),
    "numerics": re.compile(
        r"\bfloating[- ]point\b|\bnumerical(ly)? stable\b|\boverflow\b|\bprecision\b", re.I),
}

# Per-category weight: how much one hit contributes toward the keyword signal.
# Sums well above 1.0 on purpose — _keyword_score clips the total, so a task
# that trips several hard categories saturates rather than exceeding [0, 1].
KEYWORD_WEIGHTS = {
    "recursion": 0.35,
    "dynamic_programming": 0.55,
    "concurrency": 0.55,
    "parsing": 0.45,
    "regex": 0.25,
    "graph": 0.35,
    "numerics": 0.25,
}

# --- constraint signals -------------------------------------------------------
# Things that read as an explicit requirement the solution must satisfy.
_CONSTRAINT_PATTERNS = (
    re.compile(r"\bmust\b", re.I),
    re.compile(r"\bshould\b", re.I),
    re.compile(r"\bshall\b", re.I),
    re.compile(r"\bcannot\b|\bmust not\b|\bshould not\b|\bmay not\b", re.I),
    re.compile(r"\bat (least|most)\b", re.I),
    re.compile(r"\bno more than\b|\bno less than\b", re.I),
    re.compile(r"\bexactly\b"),
    re.compile(r"\bO\([^)]{1,20}\)"),               # complexity bound: O(n log n)
    re.compile(r"\bwithin\s+\d"),                    # "within 5 seconds/attempts"
    re.compile(r"^\s*(?:[-*]|\d+[.)])\s+\S", re.M),  # numbered/bulleted list item
    re.compile(r"\bedge case", re.I),
    re.compile(r"\bthread[- ]safe\b|\bidempotent\b|\bimmutable\b", re.I),
)


def _length_score(text, scale=400.0):
    """Saturating length signal in [0, 1); scale is the char count at ~63% of max."""
    n = len(text or "")
    return 1.0 - math.exp(-n / scale) if n else 0.0


def _keyword_hits(text):
    """Return the set of KEYWORD_CATEGORIES names that appear in text."""
    text = text or ""
    return {name for name, pat in KEYWORD_CATEGORIES.items() if pat.search(text)}


def _keyword_score(text):
    hits = _keyword_hits(text)
    total = sum(KEYWORD_WEIGHTS[name] for name in hits)
    return min(total, 1.0), hits


def _constraint_count(text):
    text = text or ""
    return sum(len(pat.findall(text)) for pat in _CONSTRAINT_PATTERNS)


def _constraint_score(count, scale=6.0):
    """Saturating constraint signal in [0, 1); scale is the count at ~63% of max."""
    return 1.0 - math.exp(-count / scale) if count else 0.0


# Combination weights, sum to 1.0. Keywords lead (the strongest signal for
# "this needs a real algorithm"), constraints second, raw length last (a long
# task can just be verbose, not hard).
WEIGHTS = {"length": 0.20, "keyword": 0.45, "constraint": 0.35}

# Routing thresholds for classify()/suggest_max_attempts().
EASY_MAX = 0.34
MEDIUM_MAX = 0.67


def estimate(task_text, weights=None):
    """Score a task's difficulty in [0, 1] from its text alone.

    Returns a dict: {score, length_score, keyword_score, constraint_score,
    keywords (sorted list of matched categories), constraint_count}.
    `weights` overrides WEIGHTS for experimentation (must contain the same
    three keys; renormalized if they don't already sum to 1).
    """
    w = dict(WEIGHTS if weights is None else weights)
    total_w = sum(w.values()) or 1.0
    w = {k: v / total_w for k, v in w.items()}

    length_s = _length_score(task_text)
    keyword_s, hits = _keyword_score(task_text)
    count = _constraint_count(task_text)
    constraint_s = _constraint_score(count)

    score = (w["length"] * length_s + w["keyword"] * keyword_s
             + w["constraint"] * constraint_s)
    score = max(0.0, min(1.0, score))
    return {
        "score": score,
        "length_score": length_s,
        "keyword_score": keyword_s,
        "constraint_score": constraint_s,
        "keywords": sorted(hits),
        "constraint_count": count,
    }


def score(task_text):
    """Convenience: just the scalar difficulty in [0, 1]."""
    return estimate(task_text)["score"]


def classify(difficulty_score):
    """Bucket a score into 'easy' | 'medium' | 'hard' for logging/dashboards."""
    if difficulty_score <= EASY_MAX:
        return "easy"
    if difficulty_score <= MEDIUM_MAX:
        return "medium"
    return "hard"


def suggest_max_attempts(task_text, base=3, min_attempts=1, max_attempts=6):
    """Map a task's difficulty to a solver.solve()-style max_attempts budget.

    Easy tasks get fewer repair attempts (they should pass fast or something
    else is wrong); hard tasks get more room to iterate. `base` is the
    attempts given to a difficulty-0.5 (medium) task; the result is clamped
    to [min_attempts, max_attempts]. Callers pass the returned int straight
    into solver.solve(..., max_attempts=suggest_max_attempts(prompt)).
    """
    d = score(task_text)
    # linear around base: 0.0 -> base-2, 0.5 -> base, 1.0 -> base+2 (then clamp)
    raw = base + round((d - 0.5) * 4)
    return max(min_attempts, min(max_attempts, int(raw)))
