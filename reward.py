"""Outcome signal -> scalar reward. Execution-grounded signals weighted highest.

SPEC-3 Phase 4: the pricing table and thresholds now live in
``sonder_runtime.domain.memory.rules``; this module stays the compatible
surface with identical names and behavior.
"""
from sonder_runtime.domain.memory import rules as _rules

SIGNAL_REWARDS = _rules.SIGNAL_REWARDS
VALID_SIGNALS = set(_rules.VALID_SIGNALS)
GOOD_THRESHOLD = _rules.GOOD_THRESHOLD

# Outcome provenance (#62). Re-exported so a root caller recording a verdict
# names the writing path from the same module it takes the reward from, and
# cannot end up inventing a source string of its own.
SOURCE_CALLER = _rules.OUTCOME_SOURCE_CALLER
SOURCE_MACHINE = _rules.OUTCOME_SOURCE_MACHINE
SOURCE_ATTRIBUTED = _rules.OUTCOME_SOURCE_ATTRIBUTED
SOURCE_SELF_CURRICULUM = _rules.OUTCOME_SOURCE_SELF_CURRICULUM
SOURCE_UNKNOWN = _rules.OUTCOME_SOURCE_UNKNOWN
VALID_SOURCES = frozenset(_rules.OUTCOME_SOURCES)


def score(signal):
    """The scalar reward for an outcome signal, from the shared rules table.

    Delegates to the single source of truth in domain.memory.rules so scores
    stay identical wherever they are read; an unknown signal is handled there,
    not here.
    """
    return _rules.reward_score(signal)


def is_good(signal):
    return _rules.reward_is_good(signal)
