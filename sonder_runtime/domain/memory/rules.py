"""Pure memory rules (SPEC-3 Phase 4 extraction).

The scoring, ranking, and thresholding logic that governs learning
outcomes and retrieval — with no I/O, no embedding transport, no SQLite.
The root modules (``reward.py``, ``mmr_rerank.py``, ``recall.py``)
delegate here and keep identical behavior; similarity is always an
injected function so this module stays free of the embedding adapter.
"""
from __future__ import annotations

from typing import Callable, Sequence

# --- outcome reward pricing -------------------------------------------------

# Execution-grounded signals weighted highest. Historical rows are canonical:
# export_training_data compares a stored reward against score() to detect
# corruption, so a signal's price must never change once shipped.
SIGNAL_REWARDS = {
    "tests_passed": 1.0,
    "used": 0.9,
    "copied": 0.85,
    "edited": 0.75,
    "accepted": 0.8,
    "compiled": 0.7,
    "rejected": -0.5,
    "failed": -1.0,
}
VALID_SIGNALS = frozenset(SIGNAL_REWARDS)
# Above "compiled" (0.70) on purpose: compiling proves the code builds, not
# that it produced the right answer, so it must not distill into a lesson or
# fine-tuning row.
GOOD_THRESHOLD = 0.71


def reward_score(signal: str) -> float:
    return SIGNAL_REWARDS.get(signal, 0.0)


def reward_is_good(signal: str) -> bool:
    return reward_score(signal) >= GOOD_THRESHOLD


# --- recall thresholding ----------------------------------------------------

# Default cosine floor for recall: genuinely close matches only.
DEFAULT_RECALL_MIN_SIM = 0.72


def passes_similarity(sim: float, min_sim: float) -> bool:
    """True when a candidate's similarity clears the recall floor."""
    return sim >= min_sim


# --- maximal marginal relevance --------------------------------------------

def mmr_select(
    query_vec,
    candidates: Sequence[tuple],
    *,
    k: int = 5,
    lambda_mult: float = 0.5,
    sim_fn: Callable,
) -> list:
    """Greedy MMR selection over (id, vector) candidates.

    Trades query relevance against redundancy with already-picked items:

        score(c) = lambda_mult * relevance(c, query)
                   - (1 - lambda_mult) * max(sim(c, selected))

    ``sim_fn`` is required (the domain layer never imports the embedding
    adapter). Returns candidate ids in selection order; exact-tie order
    favors the earlier input (stable, deterministic).
    """
    if k <= 0 or not candidates:
        return []
    if not query_vec:
        return [cid for cid, _ in candidates[:k]]
    lambda_mult = max(0.0, min(1.0, lambda_mult))

    remaining = list(range(len(candidates)))
    selected: list[int] = []
    while remaining and len(selected) < k:
        best_idx = None
        best_score = None
        for i in remaining:
            _, vec = candidates[i]
            relevance = sim_fn(query_vec, vec)
            if selected:
                redundancy = max(sim_fn(vec, candidates[j][1]) for j in selected)
            else:
                redundancy = 0.0
            score = lambda_mult * relevance - (1.0 - lambda_mult) * redundancy
            if best_score is None or score > best_score:
                best_score = score
                best_idx = i
        selected.append(best_idx)
        remaining.remove(best_idx)
    return [candidates[i][0] for i in selected]
