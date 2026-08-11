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
CALLER_JUDGED = _rules.CALLER_JUDGED

# Produced by running something. Answers "did it build / did tests pass".
EXECUTION_GROUNDED = _rules.EXECUTION_GROUNDED

# A population is a signal set AND a provenance set (#62). The signal name
# alone was never enough: `accepted` is written by a caller reviewing delegated
# work *and* by artifact_verify / ground_artifact reporting their own checks.
# Before `outcomes.source` existed the two were indistinguishable, so the one
# number claiming to measure delegated-work quality silently included machine
# verdicts. `None` means "any provenance".
#
# Consequence, stated rather than hidden: on a store whose rows predate the
# column every row is `unknown`, so the caller population measures 0 and the
# verdict is `unmeasured`. That is not a regression, it is this module's own
# rule -- ignorance fails closed -- finally applied to a question it could not
# previously ask. 9,450 rows of unrecorded provenance are not 9,450 caller
# judgements, and quoting them as one is the defect, not the measurement.
POPULATIONS = {
    "caller": (CALLER_JUDGED, _rules.CALLER_JUDGED_OUTCOME_SOURCES),
    "execution": (EXECUTION_GROUNDED, None),
}

# Below this many observations, reliability is unknown, not good.
MIN_SAMPLE = 20

# A rate under this is poor enough to demand verification before a claim.
POOR_BELOW = 0.60
# At or above this, measured reliability is good.
GOOD_AT_OR_ABOVE = 0.85

UNMEASURED, POOR, MIXED, GOOD = "unmeasured", "poor", "mixed", "good"

# The standing as an agent must be able to read it. Three states, never two.
#
# ``should_verify`` returns ``(True, reason)`` for a measured-poor record *and*
# for a record too thin to measure. The boolean is identical; the facts are not.
# "We measured you 218 times and you were right 56% of the time" and "we have
# never measured you" call for the same caution and for completely different
# reporting, and an agent handed only the boolean cannot say which one it is
# working under. That collapse is the same defect the claim reviewer had, where
# "the tool ran and found nothing" and "the tool was never permitted to run"
# arrived as one indistinguishable silence.
#
# So the state is what crosses the boundary. The reason string still carries the
# numbers; the state carries the epistemic difference.
VERIFIED_GOOD = "verified-good"     # measured, and at or above the bar
UNVERIFIED = "unverified"           # measured, and below the bar
UNVERIFIABLE = "unverifiable"       # too few observations to measure at all

STATES = (VERIFIED_GOOD, UNVERIFIED, UNVERIFIABLE)


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


def _counts(conn, sources=None) -> dict:
    import sonder_runtime.adapters.memory_store as memory_store
    try:
        return dict(memory_store.outcome_signal_counts(conn, sources) or {})
    except TypeError:
        # Test and third-party store adapters from before provenance filtering
        # accepted only ``conn``.  They still provide an authoritative count;
        # retain that compatibility rather than turn every such read into an
        # empty, fail-closed population.
        return dict(memory_store.outcome_signal_counts(conn) or {})
    except Exception:
        return {}


def measure(conn, population: str = "caller") -> Measurement:
    """Measured reliability for one named population of outcome signals.

    The population fixes both the signals counted and the provenances they may
    have come from; see POPULATIONS.
    """
    key = str(population or "caller").strip().lower()
    entry = POPULATIONS.get(key)
    if entry is None:
        raise ValueError(
            "unknown population '%s'. choose one of: %s"
            % (population, ", ".join(sorted(POPULATIONS)))
        )
    signals, sources = entry
    counts = _counts(conn, sources)
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
        # "of recorded provenance" is load-bearing, not decoration: since #62
        # this verdict has two causes -- too few judgements, or judgements
        # whose provenance was never recorded and so cannot be counted as
        # caller-judged. Both warrant verifying; conflating them in the prose
        # would hide which one the operator can actually fix.
        return True, ("only %d judged outcomes of recorded provenance (need "
                      "%d) - reliability is unknown, so verify rather than "
                      "assume" % (m.total, MIN_SAMPLE))
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


def standing(conn, population: str = "caller") -> tuple:
    """``(state, Measurement)`` for one population. Never collapses the states.

    The verdict already distinguishes ``unmeasured`` from ``poor``; this maps
    the four verdicts onto the three states a consumer has to act on, so that
    "could not be verified" cannot arrive looking like "was not verified".
    """
    m = measure(conn, population)
    if m.verdict == UNMEASURED:
        return UNVERIFIABLE, m
    if m.verdict == GOOD:
        return VERIFIED_GOOD, m
    return UNVERIFIED, m


def agent_notice(conn, population: str = "caller") -> str:
    """The standing, rendered for the agent that is about to make the claims.

    Every figure is a projection of counts. There is deliberately no rate in the
    ``unverifiable`` branch: below ``MIN_SAMPLE`` there is no rate, and printing
    one anyway is how a thin sample becomes a reassuring percentage.

    Only the named population is rendered. The self-graded execution population
    is roughly fifty times larger and forty points higher; showing both here, or
    either one unlabelled, would read as accuracy and is not one.
    """
    state, m = standing(conn, population)
    head = "VERIFICATION STANDING: %s" % state
    if state == UNVERIFIABLE:
        return "\n".join([
            head,
            "  population '%s': %d judged outcomes on record, below the %d needed"
            % (m.population, m.total, MIN_SAMPLE),
            "  to measure anything. There is no reliability figure to quote here.",
            "  This is 'could not be verified', which is NOT 'verified and fine'.",
            "  Cite a check you actually ran, or report the work as unverified.",
        ])
    rate = "%.1f%%" % (m.rate * 100)
    if state == VERIFIED_GOOD:
        tail = [
            "  Measured good, so this is 'verified and good' for past work only.",
            "  It is not evidence about this run. Say what you checked.",
        ]
    else:
        tail = [
            "  Measured below the %.0f%% bar, so treat your own output as"
            % (GOOD_AT_OR_ABOVE * 100),
            "  unverified until a check confirms it. Do not report success on",
            "  your own say-so; cite a check, or report the work as unverified.",
        ]
    return "\n".join([
        head,
        "  population '%s': %d good / %d bad (%s over n=%d) - %s"
        % (m.population, m.good, m.bad, rate, m.total, m.verdict),
        "  Self-graded execution outcomes are counted separately and are not",
        "  included in this figure.",
    ] + tail)


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
