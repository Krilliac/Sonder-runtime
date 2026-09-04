"""Memory governance: curation, dedup, decay, and capacity management.

Prevents unbounded memory accumulation that degrades agent performance.

Pure functions operating on plain data -- no database access, no embedding
transport, no external NLP libraries.  Similarity uses word-level Jaccard
on normalized tokens, which is sufficient for catching near-duplicate
memories and scoring novelty without heavyweight dependencies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MemoryPolicy:
    """Configurable limits for memory governance."""

    max_entries: int = 1000
    max_age_days: int = 90
    dedup_threshold: float = 0.85
    decay_factor: float = 0.95

    def __post_init__(self) -> None:
        if self.max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        if self.max_age_days < 1:
            raise ValueError("max_age_days must be at least 1")
        if not 0.0 <= self.dedup_threshold <= 1.0:
            raise ValueError("dedup_threshold must be between 0.0 and 1.0")
        if not 0.0 < self.decay_factor <= 1.0:
            raise ValueError("decay_factor must be in (0.0, 1.0]")


_DEFAULT_POLICY = MemoryPolicy()


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokens, stripping punctuation."""
    return set(_WORD_RE.findall(text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two token sets."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

# Length heuristics -- memories shorter than this are likely noise (a bare
# "ok" or "yes"), and ones longer than this lose focus.
_MIN_USEFUL_TOKENS = 3
_IDEAL_MIN_TOKENS = 5
_IDEAL_MAX_TOKENS = 150
_MAX_USEFUL_TOKENS = 300


def _length_score(n_tokens: int) -> float:
    """Score from 0.0 to 1.0 based on token count."""
    if n_tokens < _MIN_USEFUL_TOKENS:
        return 0.0
    if n_tokens <= _IDEAL_MIN_TOKENS:
        # ramp up linearly from min to ideal-min
        return (n_tokens - _MIN_USEFUL_TOKENS) / (_IDEAL_MIN_TOKENS - _MIN_USEFUL_TOKENS) * 0.5 + 0.5
    if n_tokens <= _IDEAL_MAX_TOKENS:
        return 1.0
    if n_tokens <= _MAX_USEFUL_TOKENS:
        # ramp down linearly from ideal-max to max
        return 1.0 - (n_tokens - _IDEAL_MAX_TOKENS) / (_MAX_USEFUL_TOKENS - _IDEAL_MAX_TOKENS) * 0.5
    return 0.5  # still usable, just long


def score_memory(
    content: str,
    existing_memories: list[str],
    policy: MemoryPolicy | None = None,
) -> float:
    """Score a candidate memory from 0.0 to 1.0.

    Combines novelty (dissimilarity from existing memories via Jaccard) and
    length quality.  Returns 0.0 if the candidate is a near-duplicate of any
    existing memory (similarity >= ``policy.dedup_threshold``).
    """
    if policy is None:
        policy = _DEFAULT_POLICY

    tokens = _tokenize(content)
    n_tokens = len(tokens)

    # Length component
    length = _length_score(n_tokens)
    if length == 0.0:
        return 0.0

    # Novelty / dedup component
    if not existing_memories:
        return length

    max_sim = 0.0
    for existing in existing_memories:
        sim = _jaccard(tokens, _tokenize(existing))
        if sim >= policy.dedup_threshold:
            return 0.0  # near-duplicate
        max_sim = max(max_sim, sim)

    novelty = 1.0 - max_sim
    return novelty * 0.7 + length * 0.3


# ---------------------------------------------------------------------------
# Admission gate
# ---------------------------------------------------------------------------

_ADMISSION_THRESHOLD = 0.3


def should_store(
    content: str,
    existing_memories: list[str],
    policy: MemoryPolicy | None = None,
) -> bool:
    """Gate function: should this candidate be admitted to long-term memory?"""
    if not content or not content.strip():
        return False
    return score_memory(content, existing_memories, policy) >= _ADMISSION_THRESHOLD


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------

def _parse_ts(value) -> datetime | None:
    """Best-effort parse of a timestamp field to a UTC datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(value, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
    return None


def eviction_candidates(
    memories: list[dict],
    policy: MemoryPolicy | None = None,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Return IDs of memories that should be evicted.

    A memory is a candidate for eviction when:
    - It exceeds ``max_age_days``, or
    - The total count exceeds ``max_entries`` (lowest-scored evicted first).

    Each dict must have at least ``"id"`` and ``"content"`` keys.
    ``"created_at"`` (datetime or ISO string) is used for age checks;
    ``"score"`` (float) is an optional pre-computed relevance score used
    for capacity tie-breaking (defaults to scoring against peers).
    """
    if policy is None:
        policy = _DEFAULT_POLICY
    if now is None:
        now = datetime.now(timezone.utc)

    evict_ids: set[str] = set()
    max_age_secs = policy.max_age_days * 86400

    # --- age-based eviction ---
    for mem in memories:
        ts = _parse_ts(mem.get("created_at"))
        if ts is not None and (now - ts).total_seconds() > max_age_secs:
            evict_ids.add(mem["id"])

    # --- capacity-based eviction ---
    remaining = [m for m in memories if m["id"] not in evict_ids]
    if len(remaining) > policy.max_entries:
        # Score remaining memories for ranking
        all_contents = [m["content"] for m in remaining]
        scored: list[tuple[float, str]] = []
        for m in remaining:
            if "score" in m:
                scored.append((float(m["score"]), m["id"]))
            else:
                others = [c for c in all_contents if c != m["content"]]
                s = score_memory(m["content"], others, policy)
                scored.append((s, m["id"]))

        scored.sort(key=lambda x: x[0])
        n_to_evict = len(remaining) - policy.max_entries
        for _, mid in scored[:n_to_evict]:
            evict_ids.add(mid)

    return list(evict_ids)


# ---------------------------------------------------------------------------
# Curation
# ---------------------------------------------------------------------------

def curate(
    memories: list[dict],
    policy: MemoryPolicy | None = None,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Return the curated subset of memories.

    Applies the full governance pipeline: age eviction, deduplication,
    and capacity enforcement.  The returned list preserves input order
    for surviving entries.
    """
    if policy is None:
        policy = _DEFAULT_POLICY

    evict_ids = set(eviction_candidates(memories, policy, now=now))

    surviving = [m for m in memories if m["id"] not in evict_ids]

    # --- dedup pass on survivors ---
    kept: list[dict] = []
    kept_contents: list[str] = []
    for m in surviving:
        tokens = _tokenize(m["content"])
        is_dup = False
        for existing in kept_contents:
            if _jaccard(tokens, _tokenize(existing)) >= policy.dedup_threshold:
                is_dup = True
                break
        if not is_dup:
            kept.append(m)
            kept_contents.append(m["content"])

    return kept
