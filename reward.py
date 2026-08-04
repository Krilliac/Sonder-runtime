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
    return _rules.reward_score(signal)


def is_good(signal):
    return _rules.reward_is_good(signal)
