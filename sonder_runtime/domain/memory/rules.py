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

# Signal populations answer distinct questions and are never averaged.
CALLER_JUDGED = frozenset({"used", "copied", "edited", "accepted", "rejected"})
EXECUTION_GROUNDED = frozenset({"tests_passed", "compiled", "failed"})
POPULATIONS = {"caller": CALLER_JUDGED, "execution": EXECUTION_GROUNDED}
_POPULATION_TIER = {"caller": 2, "execution": 1}

# Below every stored reward: absence of evidence cannot outrank measurement.
NO_MEASUREMENT_RANK = -2.0


def reward_score(signal: str) -> float:
    return SIGNAL_REWARDS.get(signal, 0.0)


def reward_is_good(signal: str) -> bool:
    return reward_score(signal) >= GOOD_THRESHOLD


def signal_population(signal: str) -> str:
    """Return ``caller`` or ``execution`` for a known outcome signal."""
    for name, signals in POPULATIONS.items():
        if signal in signals:
            return name
    return ""


def evidence_rank(signal: str) -> tuple[int, int, float]:
    """Credit order: eligibility, then positive evidence population, then price."""
    good = reward_is_good(signal)
    return (
        1 if good else 0,
        _POPULATION_TIER.get(signal_population(signal), 0) if good else 0,
        reward_score(signal),
    )


def retention_rank(caller_mean, execution_mean) -> tuple[float, float]:
    """Ascending duplicate-retention order, preserving caller evidence first."""
    def rank(mean):
        return NO_MEASUREMENT_RANK if mean is None else float(mean)
    return (-rank(caller_mean), -rank(execution_mean))


# --- outcome provenance -----------------------------------------------------

# WHO produced a verdict, recorded by the writing path rather than asserted by
# the caller. Without it a machine verdict is byte-identical to a human
# judgement forever: three separate lanes could not quantify their own findings
# because no query could separate the populations, and `accepted` is written
# both by a caller reviewing delegated work and by artifact_verify.
#
# The value is a property of the WRITER, never a parameter a caller chooses.

# Recorded through the caller-facing `record_outcome` tool: something with
# judgement looked at the work and said so. NOT a promise that the something is
# human -- see OUTCOME_SOURCE_SELF_CURRICULUM for the runtime's own drivers.
OUTCOME_SOURCE_CALLER = "caller"
# The runtime graded THIS interaction's own output by running it -- the code
# gate executing the reply's code and finding it broken. Nobody judged, but the
# link between the verdict and the work is direct and not in doubt.
OUTCOME_SOURCE_MACHINE = "machine"
# The runtime matched a LATER verification back to a pending generation, by
# project + time window + run id (`grounded_outcomes.note_generation` ->
# `attribute`). Execution-grounded like `machine`, but the link to the work is
# a heuristic: two concurrent runs scoped to the same project can cross-match,
# and the rendered-verdict reader this path depends on has already been found
# inverted once, filing failing verifications as +1.0 passes. Separated from
# `machine` for exactly that reason -- see
# EVICTION_INELIGIBLE_OUTCOME_SOURCES.
OUTCOME_SOURCE_ATTRIBUTED = "attributed"
# The runtime set the task AND marked it (curriculum_run, game_ladder). It is
# execution-grounded like `machine`, but it also chose the exam, so it answers
# "how often does the runtime pass its own tests", not "is delegated work good".
OUTCOME_SOURCE_SELF_CURRICULUM = "self_curriculum"
# Written before this column existed. Not a default and not a guess: the
# provenance of those rows is genuinely unrecoverable, and an honest `unknown`
# is worth more than a plausible label that can never be audited again.
OUTCOME_SOURCE_UNKNOWN = "unknown"

OUTCOME_SOURCES = frozenset({
    OUTCOME_SOURCE_CALLER,
    OUTCOME_SOURCE_MACHINE,
    OUTCOME_SOURCE_ATTRIBUTED,
    OUTCOME_SOURCE_SELF_CURRICULUM,
    OUTCOME_SOURCE_UNKNOWN,
})

# The population that answers "was the delegated work any good". Kept here
# rather than in calibration.py because learning_health asks the same question
# and the two disagreeing is how a blended number gets quoted as an accuracy.
CALLER_JUDGED_OUTCOME_SOURCES = frozenset({OUTCOME_SOURCE_CALLER})

# Sources whose verdicts may NOT drive live lesson eviction.
#
# `lesson_quarantine` removes a lesson from retrieval on accumulated negative
# reward. The dividing line is NOT "machine versus human" -- the code gate
# running a reply's own code and finding it broken is excellent evidence that
# the lesson retrieved for that reply did not help, and it has driven the gate
# for as long as it has existed. The line is whether the verdict is KNOWN to be
# about the work the lesson informed.
#
# `attributed` is the one source where it is not: that path matches a later
# verification back to a pending generation heuristically, so a mis-match evicts
# a lesson on a failure that belongs to different work entirely. Excluding it is
# the narrow change that makes routing `server._record_outcome_signal` through
# the lesson-crediting wrapper safe -- refused before this column existed
# because nothing could tell the populations apart afterwards.
#
# Everything else keeps driving the gate exactly as it does today, deliberately:
# a filter that quietly emptied the gate's evidence would be a gate that stopped
# firing, reported as an improvement.
EVICTION_INELIGIBLE_OUTCOME_SOURCES = frozenset({OUTCOME_SOURCE_ATTRIBUTED})


def outcome_source_is_valid(source) -> bool:
    return source in OUTCOME_SOURCES


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
