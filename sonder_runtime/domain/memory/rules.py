"""Pure memory rules (SPEC-3 Phase 4 extraction).

The scoring, ranking, and thresholding logic that governs learning
outcomes and retrieval — with no I/O, no embedding transport, no SQLite.
The legacy reward/MMR modules and migrated recall adapter delegate here
and keep identical behavior; similarity is always an injected function
so this module stays free of the embedding adapter.
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

# --- the two populations ----------------------------------------------------
#
# A caller who reviewed the work and the runtime marking its own homework are
# different KINDS of evidence, and this codebase refuses to blend them (see
# calibration.py, which imports these and reports both populations side by side
# without ever producing one figure over both).
#
# The pricing table above cannot express that difference and must not try: it
# is frozen, because export_training_data compares a stored reward against
# reward_score() to detect corruption, so moving a price rewrites history. What
# it does say, read as a ranking, is exactly backwards -- self-graded
# "tests_passed" is priced 1.0, above EVERY caller-judged signal -- and the
# fine-tuning corpus was ordered by it. evidence_rank() below fixes the order
# without touching a price, by ranking the population FIRST and using the
# frozen price only as a tie-break inside it.

# Judged by a caller who reviewed the work.
CALLER_JUDGED = frozenset({"used", "copied", "edited", "accepted", "rejected"})

# Produced by running something. The runtime grading itself.
EXECUTION_GROUNDED = frozenset({"tests_passed", "compiled", "failed"})

POPULATIONS = {
    "caller": CALLER_JUDGED,
    "execution": EXECUTION_GROUNDED,
}

# Lexicographic tiers, never summed with a reward. Deliberately no entry for an
# unrecognised signal beyond the 0 default: unknown provenance ranks below both.
_POPULATION_TIER = {"caller": 2, "execution": 1}


def reward_score(signal: str) -> float:
    return SIGNAL_REWARDS.get(signal, 0.0)


def reward_is_good(signal: str) -> bool:
    """Whether a signal is evidence of good work AT ALL -- an eligibility gate.

    This is a yes/no per row and is population-blind on purpose: a passing test
    and a caller keeping the output are both evidence. It is NOT an ordering.
    Never sort or compare rows by this or by reward_score() across populations
    -- use evidence_rank(), or the strongest self-graded row displaces a
    human-validated one.
    """
    return reward_score(signal) >= GOOD_THRESHOLD


def signal_population(signal: str) -> str:
    """Which population a signal belongs to: "caller", "execution", or "" ."""
    for name, signals in POPULATIONS.items():
        if signal in signals:
            return name
    return ""


def evidence_rank(signal: str) -> tuple[int, int, float]:
    """Order two outcome signals without blending their populations.

    Three lexicographic keys, never summed:

      1. is this evidence of good work at all -- the eligibility gate;
      2. for GOOD signals only, which population produced it -- a caller who
         reviewed the work outranks the runtime grading itself;
      3. the frozen price, as a tie-break INSIDE one population.

    Key 2 is conditioned on key 1 because the population distinction is a
    statement about positive evidence: it answers "whose judgement says this
    was good". Below the eligibility bar there is no credit to attribute, and
    ranking by population there would put `rejected` (a caller's negative)
    above `compiled` (a weak positive) -- so those are ordered by price alone.

    So `edited` (0.75, a caller kept the output after changing it) outranks
    `tests_passed` (1.0, the runtime ran its own tests), while `used` still
    outranks `edited` and `tests_passed` still outranks `compiled`.

    Key 1 is what makes the ordering TOTAL, so this carries no precondition for
    its callers. Without it, `rejected` (caller, -0.5) would outrank
    `tests_passed` (execution, 1.0) -- harmless only for as long as every call
    site happens to be reward_is_good-gated, which is an unwritten contract the
    next caller cannot see. A guard that depends on being called correctly is
    not a guard.

    There is deliberately no function here that reduces the two populations to
    one number; that figure reads like quality and is not one.
    """
    good = reward_is_good(signal)
    return (
        1 if good else 0,
        _POPULATION_TIER.get(signal_population(signal), 0) if good else 0,
        reward_score(signal),
    )


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
