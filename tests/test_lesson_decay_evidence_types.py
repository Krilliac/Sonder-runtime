"""Evidence-typed half-lives in lesson_decay.

Offline and deterministic like test_lesson_decay: ages are explicit and no
store is touched. The load-bearing assertions are the backward-compatibility
ones: evidence without a mapped type must decay bit-for-bit as before.
"""
import math

import lesson_decay
import pytest
from sonder_runtime.domain.memory import rules as memory_rules

REFERENCE_HALF_LIVES = {
    "test": 180.0,
    "bug_fix": 365.0,
    "source_code": 365.0,
    "user_correction": 730.0,
    "session": 14.0,
}


def test_reference_half_life_table_is_exact():
    assert lesson_decay.EVIDENCE_TYPE_HALF_LIFE_DAYS == REFERENCE_HALF_LIVES
    # The global default the unknown path falls back to is unchanged.
    assert lesson_decay.DEFAULT_HALF_LIFE_DAYS == 30.0


@pytest.mark.parametrize("evidence_type,half_life", sorted(REFERENCE_HALF_LIVES.items()))
def test_each_evidence_type_halves_at_its_own_half_life(evidence_type, half_life):
    assert lesson_decay.half_life_for_evidence(evidence_type) == half_life
    assert math.isclose(
        lesson_decay.decayed_score(1.0, half_life, evidence_type=evidence_type), 0.5
    )
    assert math.isclose(
        lesson_decay.decayed_score(1.0, 2 * half_life, evidence_type=evidence_type), 0.25
    )


@pytest.mark.parametrize("signal,half_life", [("tests_passed", 180.0), ("compiled", 365.0)])
def test_sonder_signals_map_onto_reference_types(signal, half_life):
    assert lesson_decay.half_life_for_evidence(signal) == half_life
    assert lesson_decay.half_life_for_evidence(" %s " % signal.upper()) == half_life
    assert math.isclose(lesson_decay.decayed_score(0.8, half_life, evidence_type=signal), 0.4)


def test_signal_mapping_uses_only_real_vocabulary():
    assert set(lesson_decay.SIGNAL_EVIDENCE_TYPES.values()) <= set(REFERENCE_HALF_LIVES)
    # Every mapped key is a real Sonder outcome signal, not an invented one.
    assert set(lesson_decay.SIGNAL_EVIDENCE_TYPES) <= memory_rules.VALID_SIGNALS
    # And exactly the two whose meaning names the evidence.
    assert lesson_decay.SIGNAL_EVIDENCE_TYPES == {
        "tests_passed": "test", "compiled": "source_code",
    }


UNKNOWN_EVIDENCE = [
    None, "", "   ", "failed", "used", "copied", "edited", "accepted",
    "rejected", "unknown", "caller", "machine", "self_curriculum", "not-a-type",
    180, 1.5, b"test", ("test",), True,
]


@pytest.mark.parametrize("evidence", UNKNOWN_EVIDENCE, ids=repr)
def test_unknown_evidence_keeps_old_behaviour_bit_for_bit(evidence):
    for half_life in (lesson_decay.DEFAULT_HALF_LIFE_DAYS, 10, 0, -5, float("nan")):
        assert lesson_decay.half_life_for_evidence(evidence, half_life) is half_life
        for base in (1.0, 0.8, -0.5, 0.0, 3):
            for age in (-3, 0, 0.5, 7, 30, 60, 180, 1000):
                old = lesson_decay.decayed_score(base, age, half_life_days=half_life)
                new = lesson_decay.decayed_score(
                    base, age, half_life_days=half_life, evidence_type=evidence,
                )
                assert old.hex() == new.hex()
                old_eff = lesson_decay.effective_score(
                    base, age, 5, 3, half_life_days=half_life,
                )
                new_eff = lesson_decay.effective_score(
                    base, age, 5, 3, half_life_days=half_life, evidence_type=evidence,
                )
                assert old_eff.hex() == new_eff.hex()


def test_untyped_default_matches_the_original_formula_exactly():
    # The pre-change formula, restated: base * 0.5 ** (age / 30.0).
    for base in (1.0, 0.8, 0.7, -1.0):
        for age in (7, 30, 60, 90, 180):
            assert lesson_decay.decayed_score(base, age) == base * (0.5 ** (age / 30.0))


def test_typed_evidence_overrides_caller_half_life_only_when_known():
    assert lesson_decay.decayed_score(1.0, 10, half_life_days=10) == 0.5
    assert lesson_decay.decayed_score(
        1.0, 10, half_life_days=10, evidence_type="accepted",
    ) == 0.5
    assert math.isclose(
        lesson_decay.decayed_score(1.0, 180, half_life_days=10, evidence_type="tests_passed"),
        0.5,
    )


def test_typed_evidence_orders_by_half_life():
    at_90 = {
        kind: lesson_decay.decayed_score(1.0, 90, evidence_type=kind)
        for kind in ("session", "accepted", "tests_passed", "compiled", "user_correction")
    }
    assert at_90["accepted"] == lesson_decay.decayed_score(1.0, 90)
    assert (at_90["session"] < at_90["accepted"] < at_90["tests_passed"]
            < at_90["compiled"] < at_90["user_correction"])


def _reference_rank(lessons, now_days, half_life_days):
    """The pre-change ranking, restated independently of rank_lessons."""
    def key(pair):
        idx, lesson = pair
        created = float(lesson.get("created_days", 0))
        age = max(0.0, now_days - created)
        aged = float(lesson.get("score", 0.0)) * (0.5 ** (age / half_life_days))
        credit = lesson_decay.usage_credit(lesson.get("uses", 0), lesson.get("hits", 0))
        return (-(aged + credit), -created, idx)
    return [lesson for _i, lesson in sorted(enumerate(lessons), key=key)]


def test_rank_lessons_untyped_ordering_unchanged():
    lessons = [
        {"id": "a", "score": 1.0, "created_days": 0, "uses": 0, "hits": 0},
        {"id": "b", "score": 0.9, "created_days": 40, "uses": 3, "hits": 1},
        {"id": "c", "score": 0.6, "created_days": 90, "uses": 10, "hits": 10},
        {"id": "d", "score": 0.6, "created_days": 90, "uses": 10, "hits": 10},
        {"id": "e", "score": -0.5, "created_days": 95, "uses": 2, "hits": 0},
        {"id": "f", "score": 0.8, "created_days": 70, "uses": 0, "hits": 0,
         "evidence_type": "accepted"},
        {"id": "g", "score": 0.8, "created_days": 70, "uses": 0, "hits": 0,
         "evidence_type": None},
        {"id": "h", "score": 0.8, "created_days": 70, "uses": 0, "hits": 0,
         "evidence_type": "failed"},
    ]
    for now in (0, 7, 30, 100, 180, 400):
        for hl in (30.0, 7.0):
            got = lesson_decay.rank_lessons(lessons, now_days=now, half_life_days=hl)
            want = _reference_rank(lessons, now, hl)
            assert [lesson["id"] for lesson in got] == [lesson["id"] for lesson in want]


def test_rank_lessons_uses_evidence_type_when_present():
    lessons = [
        {"id": "untyped", "score": 1.0, "created_days": 0, "uses": 0, "hits": 0},
        {"id": "fresh", "score": 0.3, "created_days": 80, "uses": 0, "hits": 0},
        {"id": "tested", "score": 1.0, "created_days": 0, "uses": 0, "hits": 0,
         "evidence_type": "tests_passed"},
    ]
    ranked = [lesson["id"] for lesson in lesson_decay.rank_lessons(lessons, now_days=90)]
    # 90 days: untyped 1.0*0.125, fresh 0.3*0.79, tested 1.0*0.71.
    assert ranked == ["tested", "fresh", "untyped"]
