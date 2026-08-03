"""Outcome signal -> scalar reward. Execution-grounded signals weighted highest."""

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
VALID_SIGNALS = set(SIGNAL_REWARDS)
# Above "compiled" (0.70), deliberately. Compiling proves the code builds, not
# that it produced the right answer, so crediting it as success would distill a
# lesson from — and export as fine-tuning data — output that was never run.
# Raising the bar rather than lowering "compiled" keeps historical rows
# canonical: export_training_data compares a stored reward against score() to
# detect corruption, and re-pricing a signal would make every past row of it
# look corrupt instead of merely not-good-enough.
GOOD_THRESHOLD = 0.71


def score(signal):
    return SIGNAL_REWARDS.get(signal, 0.0)


def is_good(signal):
    return score(signal) >= GOOD_THRESHOLD
