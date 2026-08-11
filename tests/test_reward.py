import reward
from sonder_runtime.domain.memory import rules


def test_known_signals_score():
    assert reward.score("tests_passed") == 1.0
    assert reward.score("used") == 0.9
    assert reward.score("copied") == 0.85
    assert reward.score("edited") == 0.75
    assert reward.score("failed") == -1.0


def test_unknown_signal_is_zero():
    assert reward.score("banana") == 0.0


def test_is_good_threshold():
    assert reward.is_good("tests_passed") is True
    assert reward.is_good("edited") is True     # 0.75, above the bar
    assert reward.is_good("rejected") is False
    assert reward.is_good("failed") is False


def test_compiled_is_not_success():
    """Compiling proves the code builds, not that it produced the right
    answer. Crediting it would distill a lesson from - and export as
    fine-tuning data - output that was never run."""
    assert reward.score("compiled") == 0.7
    assert reward.GOOD_THRESHOLD > reward.score("compiled")
    assert reward.is_good("compiled") is False


def test_valid_signals_set():
    assert "accepted" in reward.VALID_SIGNALS
    assert "copied" in reward.VALID_SIGNALS
    assert "banana" not in reward.VALID_SIGNALS


# --- two populations, ordered, never averaged -------------------------------


def test_every_valid_signal_belongs_to_exactly_one_population():
    caller = rules.CALLER_JUDGED
    execution = rules.EXECUTION_GROUNDED
    assert caller.isdisjoint(execution)
    assert caller | execution == set(rules.VALID_SIGNALS)
    for signal in rules.VALID_SIGNALS:
        assert rules.signal_population(signal) in ("caller", "execution")
    assert rules.signal_population("banana") == ""


def test_a_caller_who_reviewed_the_work_outranks_the_runtime_grading_itself():
    """The defect: tests_passed is priced 1.0, above EVERY caller-judged
    signal, so a corpus ordered by price puts the runtime marking its own
    homework ahead of a human who looked at the output."""
    assert rules.evidence_rank("edited") > rules.evidence_rank("tests_passed")
    assert rules.evidence_rank("accepted") > rules.evidence_rank("tests_passed")
    assert rules.evidence_rank("used") > rules.evidence_rank("tests_passed")


def test_ranking_is_lexicographic_by_population_never_arithmetic_across_it():
    """Tier first, frozen price only as a tie-break INSIDE a population.

    A single blended number over both is the thing this codebase refuses to
    compute anywhere; there must be no such function here either.
    """
    _good, population, _price = 0, 1, 2
    assert (
        rules.evidence_rank("used")[population]
        != rules.evidence_rank("tests_passed")[population]
    )
    # inside one population the shipped price still orders
    assert rules.evidence_rank("used") > rules.evidence_rank("edited")
    assert rules.evidence_rank("tests_passed") > rules.evidence_rank("compiled")
    assert not hasattr(rules, "overall")
    assert not hasattr(rules, "combined")


def test_the_ordering_is_total_and_needs_no_precondition_from_its_callers():
    """A guard that depends on being called correctly is not a guard.

    Ranking on (population, price) alone puts `rejected` (caller, -0.5) above
    `tests_passed` (execution, 1.0). That is harmless only while every call
    site happens to be is_good-gated -- an unwritten precondition the next
    caller cannot see. Eligibility is therefore the FIRST key, so the order is
    right for any pair of signals rather than only the gated ones.
    """
    assert rules.evidence_rank("tests_passed") > rules.evidence_rank("rejected")
    assert rules.evidence_rank("compiled") > rules.evidence_rank("rejected")
    assert rules.evidence_rank("edited") > rules.evidence_rank("failed")
    assert rules.evidence_rank("banana") < rules.evidence_rank("compiled")

    good = sorted(s for s in rules.VALID_SIGNALS if rules.reward_is_good(s))
    bad = sorted(s for s in rules.VALID_SIGNALS if not rules.reward_is_good(s))
    assert good and bad
    for winner in good:
        for loser in bad:
            assert rules.evidence_rank(winner) > rules.evidence_rank(loser)


def test_the_frozen_prices_are_untouched_by_the_ranking_fix():
    """export_training_data compares a stored reward against score() to detect
    corruption, so a shipped price can never move to express a ranking."""
    assert rules.SIGNAL_REWARDS == {
        "tests_passed": 1.0, "used": 0.9, "copied": 0.85, "edited": 0.75,
        "accepted": 0.8, "compiled": 0.7, "rejected": -0.5, "failed": -1.0,
    }
    assert rules.GOOD_THRESHOLD == 0.71


def test_calibration_and_the_rules_share_one_population_taxonomy():
    """Two copies of the split would drift; calibration's is the same object."""
    import calibration

    assert calibration.CALLER_JUDGED is rules.CALLER_JUDGED
    assert calibration.EXECUTION_GROUNDED is rules.EXECUTION_GROUNDED
