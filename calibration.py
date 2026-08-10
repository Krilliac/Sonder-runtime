"""Measured reliability, and the decisions that hang off it.

This is the deliberately unglamorous version of self-awareness: a number
derived from counting, never a sentiment the model produces about itself. A
system that *generates* "I feel uncertain" is writing plausible text about its
own state with nothing underneath -- the same recall failure that makes a small
model confidently invent a lookup table. A system that *counts* "wrong 91 times
in 192 judged attempts" has a state worth acting on, and the sentence, if one is
wanted at all, is only a rendering of the count.

Two rules make it honest:

**Populations are never averaged.** ``tests_passed`` from the self-graded
curriculum and ``rejected`` from a caller reviewing delegated work answer
different questions. The store currently holds 8,883 of the former and 192 rows
of the latter; a combined figure is ~96% and means nothing. There is
deliberately no function here that returns one number for everything -- callers
must name the population, because choosing it is the whole question.

**Ignorance fails closed.** Below ``MIN_SAMPLE`` observations the verdict is
``unmeasured`` and ``should_verify`` returns True. Not knowing how reliable
something is warrants exactly the same caution as knowing it is unreliable;
treating a thin sample as a pass is how "no evidence of failure" quietly
becomes "evidence of no failure".

Stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass

from sonder_runtime.domain.memory import rules as _rules

# Judged by a caller who reviewed the work. The population that answers "was
# the delegated work any good".
CALLER_JUDGED = frozenset({"used", "copied", "edited", "accepted", "rejected"})

# Produced by running something. Answers "did it build / did tests pass".
EXECUTION_GROUNDED = frozenset({"tests_passed", "compiled", "failed"})

POPULATIONS = {
    "caller": CALLER_JUDGED,
    "execution": EXECUTION_GROUNDED,
}

# Below this many observations, reliability is unknown, not good.
MIN_SAMPLE = 20

# A rate under this is poor enough to demand verification before a claim.
POOR_BELOW = 0.60
# At or above this, measured reliability is good.
GOOD_AT_OR_ABOVE = 0.85

UNMEASURED, POOR, MIXED, GOOD = "unmeasured", "poor", "mixed", "good"


@dataclass(frozen=True)
class Measurement:
    population: str
    good: int
    bad: int
    rate: float | None      # None when there is not enough to measure
    verdict: str

    @property
    def total(self) -> int:
        return self.good + self.bad

    def to_dict(self) -> dict:
        return {"population": self.population, "good": self.good, "bad": self.bad,
                "total": self.total, "rate": self.rate, "verdict": self.verdict}

    def __str__(self) -> str:
        if self.rate is None:
            return "%s: %d observations - too few to measure (need %d)" % (
                self.population, self.total, MIN_SAMPLE)
        return "%s: %d good / %d bad  (%.1f%%, n=%d) - %s" % (
            self.population, self.good, self.bad, self.rate * 100,
            self.total, self.verdict)


def _counts(conn) -> dict:
    import sonder_runtime.adapters.memory_store as memory_store
    try:
        return dict(memory_store.outcome_signal_counts(conn) or {})
    except Exception:
        return {}


def measure(conn, population: str = "caller") -> Measurement:
    """Measured reliability for one named population of outcome signals."""
    key = str(population or "caller").strip().lower()
    signals = POPULATIONS.get(key)
    if signals is None:
        raise ValueError(
            "unknown population '%s'. choose one of: %s"
            % (population, ", ".join(sorted(POPULATIONS)))
        )
    counts = _counts(conn)
    good = sum(n for s, n in counts.items()
               if s in signals and _rules.reward_is_good(s))
    bad = sum(n for s, n in counts.items()
              if s in signals and not _rules.reward_is_good(s))
    total = good + bad
    if total < MIN_SAMPLE:
        return Measurement(key, good, bad, None, UNMEASURED)
    rate = good / total
    verdict = POOR if rate < POOR_BELOW else (
        GOOD if rate >= GOOD_AT_OR_ABOVE else MIXED)
    return Measurement(key, good, bad, rate, verdict)


def should_verify(conn, population: str = "caller") -> tuple:
    """Whether to insist on verification before claiming work is done.

    This is the load-bearing part. A confidence figure that only decorates a
    reply is theatre; the point of measuring is that a poor or unmeasured
    record *changes what happens next*.

    Returns ``(bool, reason)``.
    """
    m = measure(conn, population)
    if m.verdict == UNMEASURED:
        return True, ("only %d judged outcomes on record (need %d) - reliability "
                      "is unknown, so verify rather than assume" % (m.total, MIN_SAMPLE))
    if m.verdict == POOR:
        return True, ("measured %.0f%% good over %d judged outcomes - below the "
                      "%.0f%% bar, so verify before claiming done"
                      % (m.rate * 100, m.total, POOR_BELOW * 100))
    if m.verdict == MIXED:
        return True, ("measured %.0f%% good over %d judged outcomes - good enough "
                      "to proceed, not good enough to skip checking"
                      % (m.rate * 100, m.total))
    return False, ("measured %.0f%% good over %d judged outcomes"
                   % (m.rate * 100, m.total))


def caution(conn, population: str = "caller") -> str:
    """One line of measured caution, or empty when the record does not warrant it.

    Every word here is a projection of the counts above. Nothing is generated.
    """
    verify, reason = should_verify(conn, population)
    return reason if verify else ""


def report(conn) -> str:
    """Both populations side by side, never combined into one figure."""
    lines = ["sonder calibration", ""]
    for name in ("caller", "execution"):
        m = measure(conn, name)
        lines.append("  " + str(m))
    lines.append("")
    verify, reason = should_verify(conn, "caller")
    lines.append("  verify before claiming done: %s" % ("yes" if verify else "no"))
    lines.append("  %s" % reason)
    lines += [
        "",
        "  These are not averaged on purpose. Self-graded curriculum runs and",
        "  caller-reviewed work answer different questions; one number over both",
        "  reads like accuracy and is not one.",
    ]
    return "\n".join(lines)
