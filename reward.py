"""Outcome signal -> scalar reward. Execution-grounded signals weighted highest.

SPEC-3 Phase 4: the pricing table and thresholds now live in
``sonder_runtime.domain.memory.rules``; this module stays the compatible
surface with identical names and behavior.
"""
from sonder_runtime.domain.memory import rules as _rules

SIGNAL_REWARDS = _rules.SIGNAL_REWARDS
VALID_SIGNALS = set(_rules.VALID_SIGNALS)
GOOD_THRESHOLD = _rules.GOOD_THRESHOLD


def score(signal):
    """The scalar reward for an outcome signal, from the shared rules table.

    Delegates to the single source of truth in domain.memory.rules so scores
    stay identical wherever they are read; an unknown signal is handled there,
    not here.
    """
    return _rules.reward_score(signal)


def is_good(signal):
    """Eligibility only. To ORDER rows, use evidence_rank -- see the rules."""
    return _rules.reward_is_good(signal)


def signal_population(signal):
    """"caller", "execution", or "" -- which kind of evidence this signal is."""
    return _rules.signal_population(signal)


def evidence_rank(signal):
    """Population first, frozen price only as a tie-break inside it.

    Ordering by score() alone ranks the runtime's self-graded ``tests_passed``
    (1.0) above every caller-judged signal, which is how the fine-tuning corpus
    came to put self-marked rows ahead of human-validated ones.
    """
    return _rules.evidence_rank(signal)
