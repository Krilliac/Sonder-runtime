"""Reference lookup: the relevance floor and the honest Sources heading.

The search is an OR over every question word, so an unrelated long question
still found its five nearest passages.  These tests build their own corpus in
tmp_path: on-topic passages that hold distinctive words (event ids, "audit
policy", "kerberos") and filler passages that hold only common words ("server",
"client", "time", "check", "position", "update").  The model calls are always
fakes (the lane and multi fixtures of the neighbouring test files).
"""
from __future__ import annotations

import itertools
import math
import sqlite3
import types
from pathlib import Path

import pytest

import consult as consult_module
import server
import sonder_runtime.adapters.reference_lookup_lanes as lanes
import sonder_runtime.adapters.reference_lookup_service as service
import sonder_runtime.adapters.reference_lookup_state as state
from sonder_runtime.adapters.reference_index import MAX_STATS_ROWS, ReferenceIndex
from sonder_runtime.application.chat import reference_lookup as lookup
from sonder_runtime.application.chat.reference_lookup import (
    LOW_RELEVANCE_WARNING,
    LookupReason,
    passage_coverage,
)
from sonder_runtime.domain.common.errors import Cancelled
from sonder_runtime.domain.reference_lookup import selection
from sonder_runtime.domain.reference_lookup.answer_check import missing_warning
from sonder_runtime.domain.reference_lookup.models import (
    IndexState,
    Passage,
    RankedRow,
    ReferenceIndexError,
    TermStats,
)
from sonder_runtime.domain.reference_lookup.relevance import (
    MIN_COVERAGE,
    best_coverage,
    coverage,
    term_weight,
)
from sonder_runtime.domain.reference_lookup.terms import (
    MAX_QUERY_TERMS,
    fts_query,
    query_terms,
    quoted,
)
from sonder_runtime.platform import reference_library_config as config
from tests._reference_lookup_index import create_index, rule_cache_cleared

# Fixtures of the neighbouring lookup test files, reused as they are.
from tests.test_reference_lookup_lanes import (  # noqa: F401  (pytest fixtures and helpers)
    ANSWER,
    LOOKUP_MODEL,
    MARKER_RE,
    PLAIN_MODEL,
    lane,
)
from tests.test_reference_lookup_multi import (  # noqa: F401  (pytest fixtures)
    LOOKUP_MODEL as MULTI_LOOKUP_MODEL,
    models,
    retrievals,
)

# No applies rule is carried from one test to the next.
_forget_rule_cache = pytest.fixture(autouse=True)(rule_cache_cleared)

NONE_LINE = "Sources: none (no reference passage was close enough to the question)"
BIG_BUDGET = selection.PASSAGE_CHAR_LIMIT
BLANK = "\n\n"


# ---------------------------------------------------------------------------
# The corpus: 12 on-topic passages and 20 filler passages of common words
# ---------------------------------------------------------------------------

def _row(title, body, source, number):
    return (title, "", body, source, f"https://example.test/{source}/{number}",
            f"{source}/{number}.md", number)


ON_TOPIC = [
    _row("1102: The audit log was cleared",
         "Generated when the Security audit log is cleared. The client name and the time of the "
         "clearing are recorded.", "events", 1),
    _row("4719: System audit policy was changed",
         "Generated when the audit policy on a computer changes. Check the subcategory GUID and "
         "the server that applied it.", "events", 2),
    _row("4769: A Kerberos service ticket was requested",
         "Generated each time the Kerberos KDC grants a service ticket. A ticket encryption type of "
         "0x17 can point at kerberoasting.", "events", 3),
    _row("4768: A Kerberos authentication ticket (TGT) was requested",
         "Generated when the Kerberos KDC issues a ticket granting ticket for an account.",
         "events", 4),
    _row("4771: Kerberos pre-authentication failed",
         "A kerberos pre-authentication failure, often a wrong password for the account.",
         "events", 5),
    _row("4625: An account failed to log on",
         "This event is generated when a logon request fails. The status 0xC000006A means the "
         "password is wrong.", "events", 6),
    _row("4624: An account was successfully logged on",
         "Generated when a logon succeeds. Logon type 3 is a network logon to the server.",
         "events", 7),
    _row("4720: A user account was created",
         "Generated when a user account is created. Check who created it and the time.",
         "events", 8),
    _row("4672: Special privileges assigned to new logon",
         "Generated when an administrator logs on with sensitive privileges.", "events", 9),
    _row("T1003.001 OS Credential Dumping: LSASS Memory",
         "Adversaries dump credentials from LSASS process memory with mimikatz or procdump.",
         "attack", 10),
    _row("Kerberoasting Detection",
         "Sigma rule: detect kerberoasting through many service ticket requests with rc4 "
         "encryption.", "sigma", 11),
    _row("Audit Log Cleared via wevtutil", "Sigma rule: wevtutil cl security clears the audit log.",
         "sigma", 12),
]
COMMON_WORDS = ("server", "client", "time", "check", "position", "update")
FILLER = [
    _row(f"Notes {n}: {trio[0]} {trio[1]} handling",
         f"Notes on the {trio[0]}, the {trio[1]} and the {trio[2]}. Remember to check the "
         f"{trio[2]} before you update it.", "notes", 20 + n)
    for n, trio in enumerate(itertools.combinations(COMMON_WORDS, 3))
]
ROWS = ON_TOPIC + FILLER
assert len(ROWS) == 32

# A long question about another subject: it shares only common words with the library.
OFF_QUESTION = (
    "We are designing a multiplayer game where the server and the client must agree on the position "
    "of every player at the same time. How should we check the time of each update, and which "
    "rollback netcode, interpolation, prediction, lag compensation, matchmaking, lobby, tickrate, "
    "snapshot, delta compression, reconciliation, authoritative physics and jitter buffer approach "
    "keeps the position correct over a slow network?")
OFF_TERMS = "rollback netcode\nmatchmaking\ntickrate"
ON_QUESTION = ("What does event 1102 mean when the audit log was cleared, and which audit policy "
               "change should I check?")
ON_TERMS = "audit log cleared\n1102"
# Hits both common words and one distinctive word: the model's own terms decide.
MIXED_QUESTION = "How do I check the server time and the client position update after kerberos?"
MIXED_ON_TERMS = "kerberos ticket, 4769, kerberoasting"
MIXED_OFF_TERMS = "rollback netcode, matchmaking, tickrate, jitter"


@pytest.fixture
def path(tmp_path) -> Path:
    return create_index(tmp_path / "idx" / "floor.sqlite", ROWS)


@pytest.fixture
def index(path) -> ReferenceIndex:
    return ReferenceIndex(path)


def _rowid(index, title):
    conn = sqlite3.connect(str(index.path))
    try:
        (found,) = conn.execute("SELECT rowid FROM passages WHERE title = ?", (title,)).fetchone()
    finally:
        conn.close()
    return found


def _expand_with(reply):
    return lambda prompt: reply


def _retrieve(index, question, reply=None, *, min_coverage=0.0, k=5):
    expand = None if reply is None else _expand_with(reply)
    return lookup.retrieve(question, expand, k, BIG_BUDGET, index=index, min_coverage=min_coverage)


def _coverage_of(index, question, bundle_or_terms, passages):
    return passage_coverage(index, query_terms(question, bundle_or_terms), passages)


# ---------------------------------------------------------------------------
# Domain maths: relevance.py
# ---------------------------------------------------------------------------

def test_the_floor_constant_is_a_fifth():
    assert MIN_COVERAGE == 0.20


def test_term_weight_is_always_positive():
    for total, matching in [(0, 0), (1, 0), (1, 1), (10, 10), (8908, 0), (8908, 8908), (100, 50)]:
        assert term_weight(total, matching) > 0


def test_term_weight_rarer_is_heavier_and_an_absent_term_is_heaviest():
    weights = [term_weight(100, matching) for matching in (0, 1, 5, 50, 100)]
    assert weights == sorted(weights, reverse=True)
    assert len(set(weights)) == len(weights)
    assert term_weight(100, 0) > term_weight(100, 1)


def test_term_weight_is_the_bm25_idf_for_known_values():
    # BM25 idf: ln((N - n + 0.5) / (n + 0.5) + 1).  N=10, n=0 -> ln(22); N=10, n=4 -> ln(6.5/4.5 + 1).
    assert term_weight(10, 0) == pytest.approx(math.log(22.0))
    assert term_weight(10, 4) == pytest.approx(math.log(6.5 / 4.5 + 1.0))
    assert term_weight(10, 10) == pytest.approx(math.log(0.5 / 10.5 + 1.0))


def test_term_weight_clamps_matching_to_the_passage_count_and_negatives_to_zero():
    assert term_weight(10, 25) == term_weight(10, 10)
    assert term_weight(10, -3) == term_weight(10, 0)
    assert term_weight(-7, 0) == term_weight(0, 0)
    assert term_weight(-7, 5) == term_weight(0, 0) > 0


def test_coverage_empty_weights_is_zero():
    assert coverage({}, {"a"}) == 0.0
    assert coverage({}, set()) == 0.0


def test_coverage_nothing_held_is_zero_and_everything_held_is_one():
    weights = {"a": 1.0, "b": 3.0}
    assert coverage(weights, set()) == 0.0
    assert coverage(weights, {"a", "b"}) == 1.0


def test_coverage_is_the_weighted_share():
    weights = {"a": 1.0, "b": 3.0}
    assert coverage(weights, {"a"}) == pytest.approx(0.25)
    assert coverage(weights, {"b"}) == pytest.approx(0.75)


def test_coverage_ignores_a_held_term_that_has_no_weight():
    weights = {"a": 1.0, "b": 3.0}
    assert coverage(weights, {"a", "zzz"}) == pytest.approx(0.25)
    assert coverage(weights, {"zzz"}) == 0.0


def test_best_coverage_is_the_maximum_and_zero_without_passages():
    weights = {"a": 1.0, "b": 3.0}
    assert best_coverage(weights, []) == 0.0
    assert best_coverage(weights, iter(())) == 0.0
    assert best_coverage(weights, [{"a"}, {"b"}, set()]) == pytest.approx(0.75)
    assert best_coverage(weights, [set(), set()]) == 0.0


# ---------------------------------------------------------------------------
# query_terms and fts_query
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, extra", [
    ("What does event 4625 mean?", ()),
    ("failed logon on the server", ["security log cleared", "4625"]),
    ("", ["kerberos"]),
    (None, ()),
    ("the and of", ()),
    ("audit AUDIT Audit policy", ["audit policy"]),
    ('weird "quote', ['a "b" c']),
])
def test_fts_query_is_the_quoted_query_terms_joined_by_or(text, extra):
    assert fts_query(text, extra) == " OR ".join(quoted(t) for t in query_terms(text, extra))


def test_query_terms_drop_stop_words_but_keep_numbers_and_lowercase():
    assert query_terms("What does event 4625 mean on the Server?") == ["event", "4625", "server"]
    assert query_terms("the and of") == []
    assert query_terms(None) == []


def test_query_terms_put_the_phrase_before_its_words_and_skip_repeats():
    assert query_terms("logon", ["failed logon"]) == ["logon", "failed logon", "failed"]
    assert query_terms("Audit audit", ["AUDIT"]) == ["audit"]


def test_query_terms_are_capped():
    text = " ".join(f"word{n}" for n in range(MAX_QUERY_TERMS + 25))
    terms = query_terms(text)
    assert len(terms) == MAX_QUERY_TERMS
    assert terms == [f"word{n}" for n in range(MAX_QUERY_TERMS)]
    assert fts_query(text).count(" OR ") == MAX_QUERY_TERMS - 1
    # Extra terms count against the same cap.
    assert len(query_terms(text, ["more terms here"])) == MAX_QUERY_TERMS


def test_quoted_doubles_an_embedded_double_quote():
    assert quoted("plain") == '"plain"'
    assert quoted('a"b') == '"a""b"'
    assert quoted('a" OR "b') == '"a"" OR ""b"'
    # A quote inside an extra phrase never reaches fts_query unquoted.
    query = fts_query("x", ['say "hi" there'])
    assert all(part.startswith('"') and part.endswith('"') for part in query.split(" OR "))


# ---------------------------------------------------------------------------
# term_stats on a built index
# ---------------------------------------------------------------------------

def test_term_stats_counts_passages_and_matches(index):
    stats = index.term_stats(["1102", "kerberos", "audit", "server", "zzzqqq"], [])
    assert isinstance(stats, TermStats)
    assert stats.passage_count == 32
    # Hand-counted from the corpus: one id, three kerberos titles, audit in 1102 / 4719 / wevtutil,
    # "server" in 4719 and 4624 plus ten of the twenty filler passages, and a word nobody holds.
    assert stats.matching == {"1102": 1, "kerberos": 3, "audit": 3, "server": 12, "zzzqqq": 0}
    assert stats.held == {}


def test_term_stats_matches_through_the_porter_tokenizer(tmp_path):
    rows = [_row("Clearing the log", "Clearing the audit trail is hostile.", "events", 1),
            _row("Unrelated", "Nothing about it.", "events", 2)]
    idx = ReferenceIndex(create_index(tmp_path / "stem.sqlite", rows))
    first = _rowid(idx, "Clearing the log")
    other = _rowid(idx, "Unrelated")
    stats = idx.term_stats(["cleared", "clear", "unrelated"], [first, other])
    assert stats.matching == {"cleared": 1, "clear": 1, "unrelated": 1}
    assert stats.held[first] == frozenset({"cleared", "clear"})
    assert stats.held[other] == frozenset({"unrelated"})


def test_term_stats_held_says_which_row_holds_which_term(index):
    cleared = _rowid(index, "1102: The audit log was cleared")
    kerberos = _rowid(index, "4769: A Kerberos service ticket was requested")
    filler = _rowid(index, "Notes 0: server client handling")
    stats = index.term_stats(["audit", "kerberos", "1102", "client"], [cleared, kerberos, filler])
    assert stats.held == {
        cleared: frozenset({"audit", "1102", "client"}),
        kerberos: frozenset({"kerberos"}),
        filler: frozenset({"client"}),
    }


def test_term_stats_a_term_phrase_is_matched_as_a_phrase(index):
    cleared = _rowid(index, "1102: The audit log was cleared")
    stats = index.term_stats(["audit log", "log audit"], [cleared])
    assert stats.matching["audit log"] == 2        # 1102 and the wevtutil rule say "audit log"
    assert stats.matching["log audit"] == 0
    assert stats.held[cleared] == frozenset({"audit log"})


@pytest.mark.parametrize("hostile", ['a" OR "b', "NEAR(", "title:audit", "*", "audit OR kerberos",
                                     "NOT audit", '"', '""', "audit*", "(", ")", "body:1102"])
def test_term_stats_hostile_terms_never_raise_and_never_act_as_operators(index, hostile):
    cleared = _rowid(index, "1102: The audit log was cleared")
    stats = index.term_stats([hostile, "audit"], [cleared])
    assert stats.matching["audit"] == 3
    assert "audit" in stats.held[cleared]
    # A column filter, OR, NOT or prefix operator would match passages; a quoted string cannot.
    if hostile in ("title:audit", "audit OR kerberos", "NOT audit", "body:1102", 'a" OR "b',
                   "NEAR(", "*", "(", ")", '"', '""'):
        assert stats.matching[hostile] == 0
        assert hostile not in stats.held[cleared]
    # The string is only a phrase of its words: "audit*" is the phrase "audit", as for a search.
    if hostile == "audit*":
        assert stats.matching[hostile] == 3


def test_term_stats_ignores_duplicate_empty_and_non_string_terms(index):
    stats = index.term_stats(["audit", "audit", "", None, 5, b"audit", ("audit",), "kerberos"], [])
    assert list(stats.matching) == ["audit", "kerberos"]
    assert stats.matching == {"audit": 3, "kerberos": 3}


def test_term_stats_ignores_duplicate_invalid_and_bool_rowids(index):
    cleared = _rowid(index, "1102: The audit log was cleared")
    stats = index.term_stats(["audit"], [cleared, cleared, 0, -3, True, False, 2.0, "2", None])
    assert list(stats.held) == [cleared]
    assert stats.held[cleared] == frozenset({"audit"})
    assert index.term_stats(["audit"], [True, 0, -1]).held == {}


def test_term_stats_a_row_that_is_not_in_the_index_holds_nothing(index):
    stats = index.term_stats(["audit"], [9999])
    assert stats.held == {9999: frozenset()}


def test_term_stats_no_terms_and_no_rows_still_reports_the_index_size(index):
    stats = index.term_stats([], [])
    assert stats == TermStats(passage_count=32, matching={}, held={})


def test_term_stats_term_cap(index):
    terms = [f"w{n}" for n in range(MAX_QUERY_TERMS + 5)]
    stats = index.term_stats(terms, [])
    assert list(stats.matching) == terms[:MAX_QUERY_TERMS]
    # The cap is applied after repeats and invalid terms are removed.
    padded = ["", None, "w0", "w0"] + terms
    assert list(index.term_stats(padded, []).matching) == terms[:MAX_QUERY_TERMS]


def test_term_stats_row_cap(index):
    stats = index.term_stats(["audit"], list(range(1, 21)))
    assert MAX_STATS_ROWS == 16
    assert list(stats.held) == list(range(1, MAX_STATS_ROWS + 1))
    # Rows 1, 2 and 12 hold "audit"; rows past the cap are not reported at all.
    assert {row for row, held in stats.held.items() if held} == {1, 2, 12}


def test_term_stats_on_an_unusable_index_raises_reference_index_error(tmp_path):
    missing = ReferenceIndex(tmp_path / "absent.sqlite")
    with pytest.raises(ReferenceIndexError) as caught:
        missing.term_stats(["audit"], [1])
    assert caught.value.state is IndexState.MISSING
    text = tmp_path / "plain.txt"
    text.write_text("this is not a database", encoding="utf-8")
    with pytest.raises(ReferenceIndexError) as caught:
        ReferenceIndex(text).term_stats(["audit"], [1])
    assert caught.value.state is IndexState.NOT_SQLITE
    with pytest.raises(ReferenceIndexError):
        ReferenceIndex(tmp_path).term_stats(["audit"], [1])


def test_term_stats_a_failing_query_raises_reference_index_error(index, monkeypatch):
    def broken():
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(index, "connect", broken)
    with pytest.raises(ReferenceIndexError):
        index.term_stats(["audit"], [1])


def test_term_stats_never_writes(index, path):
    before = path.read_bytes()
    info = path.stat()
    index.term_stats(["audit", "kerberos", 'a" OR "b'], [1, 2, 3])
    index.term_stats([], [])
    assert path.read_bytes() == before
    after = path.stat()
    assert (after.st_mtime_ns, after.st_size) == (info.st_mtime_ns, info.st_size)
    assert sorted(p.name for p in path.parent.iterdir()) == ["floor.sqlite"]   # no journal or wal


# ---------------------------------------------------------------------------
# Row ids
# ---------------------------------------------------------------------------

def test_search_rows_carry_the_real_rowid(index):
    rows = index.search_rows(fts_query("audit kerberos 1102"), 20, ["1102"])
    assert rows
    assert all(row.rowid > 0 for row in rows)
    for row in rows:
        assert _rowid(index, row.title) == row.rowid
    assert {row.rowid for row in rows} >= {1, 2, 3, 4, 5, 12}


def test_title_priority_rows_carry_their_rowid_too(index):
    rows = index.search_rows(fts_query("zzzqqq"), 20, ["1102"])    # found only by the title top-up
    titles = {row.title: row.rowid for row in rows}
    assert titles["1102: The audit log was cleared"] == _rowid(index, "1102: The audit log was cleared")


def test_select_passages_copies_the_rowid_into_the_passage(index):
    rows = index.search_rows(fts_query("audit log cleared"), selection.candidate_limit(5), [])
    passages = selection.select_passages(rows, 5)
    by_title = {row.title: row.rowid for row in rows}
    assert passages and all(p.rowid == by_title[p.title] > 0 for p in passages)


def test_a_row_without_a_rowid_gives_a_passage_without_one():
    unknown = RankedRow("t", "", "b", "s", "u", "p", 1, -1.0)
    known = RankedRow("t2", "", "b2", "s", "u", "p", 2, -0.5, rowid=77)
    assert unknown.rowid == 0
    passages = selection.select_passages([unknown, known], 5)
    assert [p.rowid for p in passages] == [0, 77]


# ---------------------------------------------------------------------------
# passage_coverage and retrieve with the floor
# ---------------------------------------------------------------------------

def test_a_passage_with_a_malformed_row_id_keeps_the_passages():
    """A row id that is not a number must not turn the floor into a failed lookup."""
    from types import SimpleNamespace

    from sonder_runtime.application.chat.reference_lookup import passage_coverage

    class _Index:
        def term_stats(self, terms, rowids):  # pragma: no cover - must not be reached
            raise AssertionError("statistics were asked for a passage without a usable row id")

    for bad in (None, "7", object()):
        assert passage_coverage(_Index(), ["logon"], [SimpleNamespace(rowid=bad)]) is None


def test_the_corpus_discriminates(index):
    """If these stop holding the other tests pass or fail for the wrong reason."""
    off = _retrieve(index, OFF_QUESTION, OFF_TERMS)
    assert off.applied and off.passages
    assert _coverage_of(index, OFF_QUESTION, off.terms, off.passages) < MIN_COVERAGE
    on = _retrieve(index, ON_QUESTION, ON_TERMS)
    assert on.applied and on.passages
    assert _coverage_of(index, ON_QUESTION, on.terms, on.passages) > MIN_COVERAGE * 1.5
    # The off-topic passages are the filler ones the common words bring in.
    assert any(p.source == "notes" for p in off.passages)


def test_on_topic_question_keeps_its_passages_and_equals_the_no_floor_result(index):
    plain = _retrieve(index, ON_QUESTION, ON_TERMS)
    floored = _retrieve(index, ON_QUESTION, ON_TERMS, min_coverage=MIN_COVERAGE)
    assert floored.applied and floored.reason is LookupReason.APPLIED
    assert floored.passages == plain.passages
    assert floored == plain
    assert floored.passages[0].title == "1102: The audit log was cleared"


def test_unrelated_long_question_is_low_relevance_with_the_warning_and_no_passages(index):
    bundle = _retrieve(index, OFF_QUESTION, OFF_TERMS, min_coverage=MIN_COVERAGE)
    assert not bundle.applied
    assert bundle.reason is LookupReason.LOW_RELEVANCE
    assert bundle.reason.value == "low_relevance" and bundle.reason_text == "low_relevance"
    assert bundle.passages == ()
    assert bundle.warnings == (LOW_RELEVANCE_WARNING,)
    assert LOW_RELEVANCE_WARNING == "no reference passage was close enough to the question"
    assert bundle.index_state is IndexState.READY
    assert bundle.terms and "rollback netcode" in bundle.terms        # the terms are kept
    plain = _retrieve(index, OFF_QUESTION, OFF_TERMS)
    covered = _coverage_of(index, OFF_QUESTION, plain.terms, plain.passages)
    assert bundle.detail == f"best passage coverage {covered:.2f} is under {MIN_COVERAGE:.2f}"
    assert bundle.detail.endswith("is under 0.20")


def test_the_unrelated_question_without_a_floor_is_what_it_was(index):
    bundle = _retrieve(index, OFF_QUESTION, OFF_TERMS)
    assert bundle.applied and bundle.reason is LookupReason.APPLIED
    assert len(bundle.passages) == 5
    assert _retrieve(index, OFF_QUESTION, OFF_TERMS, min_coverage=0.0) == bundle
    assert lookup.retrieve(OFF_QUESTION, _expand_with(OFF_TERMS), 5, BIG_BUDGET, index=index) == bundle
    assert _retrieve(index, OFF_QUESTION, OFF_TERMS, min_coverage=-1.0) == bundle


def test_a_question_whose_search_finds_nothing_is_still_no_match_under_the_floor(index):
    bundle = _retrieve(index, "zzzqqq xxxyyy", None, min_coverage=MIN_COVERAGE)
    assert bundle.reason is LookupReason.NO_MATCH


def test_the_floor_is_a_strict_less_than(index):
    plain = _retrieve(index, OFF_QUESTION, OFF_TERMS)
    covered = _coverage_of(index, OFF_QUESTION, plain.terms, plain.passages)
    assert _retrieve(index, OFF_QUESTION, OFF_TERMS, min_coverage=covered).applied
    assert not _retrieve(index, OFF_QUESTION, OFF_TERMS, min_coverage=covered + 1e-6).applied


def test_the_models_extra_terms_move_the_coverage(index):
    plain = _retrieve(index, MIXED_QUESTION, None)
    good = _retrieve(index, MIXED_QUESTION, MIXED_ON_TERMS)
    bad = _retrieve(index, MIXED_QUESTION, MIXED_OFF_TERMS)
    base = _coverage_of(index, MIXED_QUESTION, plain.terms, plain.passages)
    up = _coverage_of(index, MIXED_QUESTION, good.terms, good.passages)
    down = _coverage_of(index, MIXED_QUESTION, bad.terms, bad.passages)
    assert down < MIN_COVERAGE < base < up
    # And the same question gets or loses its passages by those terms alone.
    assert _retrieve(index, MIXED_QUESTION, None, min_coverage=MIN_COVERAGE).applied
    assert _retrieve(index, MIXED_QUESTION, MIXED_ON_TERMS, min_coverage=MIN_COVERAGE).applied
    off = _retrieve(index, MIXED_QUESTION, MIXED_OFF_TERMS, min_coverage=MIN_COVERAGE)
    assert off.reason is LookupReason.LOW_RELEVANCE
    assert off.dropped_terms == bad.dropped_terms


def test_passage_coverage_matches_a_hand_computed_value(index):
    cleared = _rowid(index, "1102: The audit log was cleared")
    passage = Passage("t", "", "s", "u", "p", "x", rowid=cleared)
    # "1102" (1 passage) and "audit" (3) are held by that row, "zzzqqq" (0) is not.
    expected = (term_weight(32, 1) + term_weight(32, 3)) / (
        term_weight(32, 1) + term_weight(32, 3) + term_weight(32, 0))
    assert passage_coverage(index, ["1102", "audit", "zzzqqq"], [passage]) == pytest.approx(expected)
    assert passage_coverage(index, ["1102", "audit"], [passage]) == pytest.approx(1.0)


class _NoStats:
    """An index that offers search only: the floor cannot be measured."""

    def __init__(self, inner):
        self._inner = inner

    def status(self):
        return self._inner.status()

    def search_rows(self, match, limit, idents=()):
        return self._inner.search_rows(match, limit, idents)


class _Raising(_NoStats):
    def __init__(self, inner, error):
        super().__init__(inner)
        self._error = error

    def term_stats(self, terms, rowids):
        raise self._error


class _Garbage(_NoStats):
    def term_stats(self, terms, rowids):
        return types.SimpleNamespace(passage_count="many")


def test_passage_coverage_is_none_when_it_cannot_be_measured(index):
    passages = _retrieve(index, ON_QUESTION, ON_TERMS).passages
    terms = ["audit", "1102"]
    assert passage_coverage(_NoStats(index), terms, passages) is None
    assert passage_coverage(object(), terms, passages) is None
    assert passage_coverage(index, [], passages) is None
    assert passage_coverage(index, terms, []) is None
    unknown = [Passage("t", "", "s", "u", "p", "x"), *passages]
    assert unknown[0].rowid == 0
    assert passage_coverage(index, terms, unknown) is None
    assert passage_coverage(_Garbage(index), terms, passages) is None
    assert passage_coverage(index, terms, passages) is not None


@pytest.mark.parametrize("error", [
    RuntimeError("boom"),
    ReferenceIndexError(IndexState.UNREADABLE, "gone"),
    sqlite3.OperationalError("locked"),
    KeyError("held"),
])
def test_passage_coverage_never_raises(index, error):
    passages = _retrieve(index, ON_QUESTION, ON_TERMS).passages
    assert passage_coverage(_Raising(index, error), ["audit"], passages) is None


def test_floor_fails_open_for_an_index_without_term_stats(index):
    bundle = _retrieve(_NoStats(index), OFF_QUESTION, OFF_TERMS, min_coverage=MIN_COVERAGE)
    assert bundle.applied and bundle.reason is LookupReason.APPLIED
    assert bundle == _retrieve(index, OFF_QUESTION, OFF_TERMS)


def test_floor_fails_open_when_term_stats_raises(index):
    wrapped = _Raising(index, ReferenceIndexError(IndexState.UNREADABLE, "gone"))
    bundle = _retrieve(wrapped, OFF_QUESTION, OFF_TERMS, min_coverage=MIN_COVERAGE)
    assert bundle.applied and len(bundle.passages) == 5
    bundle = _retrieve(_Raising(index, RuntimeError("boom")), OFF_QUESTION, OFF_TERMS,
                       min_coverage=MIN_COVERAGE)
    assert bundle.applied and len(bundle.passages) == 5


def test_floor_fails_open_when_a_passage_has_no_rowid(index, monkeypatch):
    real = index.search_rows

    def without_rowids(match, limit, idents=()):
        return tuple(RankedRow(r.title, r.heading, r.body, r.source, r.url, r.path, r.doc, r.rank)
                     for r in real(match, limit, idents))

    monkeypatch.setattr(index, "search_rows", without_rowids)
    bundle = _retrieve(index, OFF_QUESTION, OFF_TERMS, min_coverage=MIN_COVERAGE)
    assert bundle.applied and all(p.rowid == 0 for p in bundle.passages)


def test_a_cancelled_search_terms_call_still_propagates_under_the_floor(index):
    def cancelled(prompt):
        raise Cancelled("the caller cancelled the turn")

    with pytest.raises(Cancelled):
        lookup.retrieve(OFF_QUESTION, cancelled, 5, BIG_BUDGET, index=index, min_coverage=MIN_COVERAGE)


def test_no_room_and_empty_question_are_decided_before_the_floor(index):
    assert lookup.retrieve("", None, 5, BIG_BUDGET, index=index,
                           min_coverage=MIN_COVERAGE).reason is LookupReason.EMPTY_QUESTION
    assert lookup.retrieve(OFF_QUESTION, None, 5, 0, index=index,
                           min_coverage=MIN_COVERAGE).reason is LookupReason.NO_ROOM


# ---------------------------------------------------------------------------
# The service: retrieve_for and the reference_search tool
# ---------------------------------------------------------------------------

@pytest.fixture
def configured(monkeypatch, path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(path.parent / "runtime_policy.json"))
    monkeypatch.setenv(config.ENV_INDEX, str(path))
    monkeypatch.delenv(config.ENV_ENABLED, raising=False)
    monkeypatch.setattr(state, "_INDEX", None)
    monkeypatch.setattr(state, "_LAST_ERROR", "")
    return path


def test_retrieve_for_applies_the_floor_only_for_an_answer(configured):
    expand = _expand_with(OFF_TERMS)
    answer = service.retrieve_for(OFF_QUESTION, expand, 5, BIG_BUDGET, for_answer=True)
    assert answer.reason is LookupReason.LOW_RELEVANCE and answer.passages == ()
    plain = service.retrieve_for(OFF_QUESTION, expand, 5, BIG_BUDGET)
    assert plain.applied and len(plain.passages) == 5
    assert service.retrieve_for(OFF_QUESTION, expand, 5, BIG_BUDGET, for_answer=False) == plain


def test_retrieve_for_an_answer_keeps_an_on_topic_question(configured):
    expand = _expand_with(ON_TERMS)
    answer = service.retrieve_for(ON_QUESTION, expand, 5, BIG_BUDGET, for_answer=True)
    assert answer.applied
    assert answer == service.retrieve_for(ON_QUESTION, expand, 5, BIG_BUDGET)


def test_retrieve_for_uses_the_module_constant_for_the_answer_floor(configured, monkeypatch):
    seen = []
    real = lookup.retrieve

    def spy(*args, **kwargs):
        seen.append(kwargs.get("min_coverage"))
        return real(*args, **kwargs)

    monkeypatch.setattr(lookup, "retrieve", spy)
    service.retrieve_for(OFF_QUESTION, None, 5, BIG_BUDGET)
    service.retrieve_for(OFF_QUESTION, None, 5, BIG_BUDGET, for_answer=True)
    assert seen == [0.0, MIN_COVERAGE]


def test_the_search_tool_still_returns_the_nearest_passages_for_any_query(configured):
    result = service.search(OFF_QUESTION, 5)
    assert result.outcome is service.SearchOutcome.FOUND
    assert "=== PASSAGE 1 [" in result.text and "Sources:" in result.text
    assert "Notes " in result.text
    on = service.search(ON_QUESTION, 5)
    assert on.outcome is service.SearchOutcome.FOUND and "1102: The audit log was cleared" in on.text


def test_the_search_tool_never_asks_for_the_floor(configured, monkeypatch):
    calls = []
    real = service.retrieve_for

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(service, "retrieve_for", spy)
    service.search(OFF_QUESTION, 5)
    assert calls and not any(kwargs.get("for_answer") for kwargs in calls)


# ---------------------------------------------------------------------------
# The chat lane end to end (fake transport)
# ---------------------------------------------------------------------------

@pytest.fixture
def floor_lane(lane, monkeypatch, path):  # noqa: F811
    """The single-model lane fixture, pointed at the floor corpus."""
    monkeypatch.setenv(config.ENV_INDEX, str(path))
    monkeypatch.setattr(state, "_INDEX", None)
    return lane


def _mcp(monkeypatch, question):
    return server.sonder(question, model_override=LOOKUP_MODEL, session="none")


def _http(monkeypatch, question):
    monkeypatch.setitem(server.TIERS, "general", LOOKUP_MODEL)
    monkeypatch.setattr(server, "_should_learn", lambda *_a: False)
    reply = server._answer_with_history_impl(question, [], tier="general")
    receipt = {}
    return lanes.finish_http(reply, receipt), receipt


def test_lane_unrelated_question_is_answered_bare_with_one_none_line(floor_lane, monkeypatch):
    floor_lane.terms = OFF_TERMS
    floor_lane.reply = "A rollback design is fine."
    text = _mcp(monkeypatch, OFF_QUESTION)

    turn = floor_lane.user_turn()
    assert turn == OFF_QUESTION
    assert "Reference passages:" not in turn and not MARKER_RE.search(turn)
    assert len(floor_lane.expansions()) == 1 and len(floor_lane.answers()) == 1
    assert text.startswith("A rollback design is fine." + BLANK + NONE_LINE)
    assert text.count("Sources:") == 1
    # The warning equals the reason shown, so it is not repeated as a Warning: line.
    assert "Warning:" not in text
    assert "Notes " not in text


def test_lane_unrelated_question_receipt_says_low_relevance(floor_lane, monkeypatch):
    floor_lane.terms = OFF_TERMS
    floor_lane.reply = "A rollback design is fine."
    text, receipt = _http(monkeypatch, OFF_QUESTION)

    assert text == "A rollback design is fine." + BLANK + NONE_LINE
    assert floor_lane.user_turn() == OFF_QUESTION
    entry = receipt[lanes.RECEIPT_KEY]
    assert entry["applied"] is False
    assert entry["reason"] == "low_relevance"
    assert entry[lanes.RECEIPT_SYSTEM_PROMPT_KEY] is False
    assert "passages" not in entry


def test_lane_receipt_entry_for_the_record_of_a_low_relevance_turn(floor_lane, monkeypatch):
    floor_lane.terms = OFF_TERMS
    monkeypatch.setitem(server.TIERS, "general", LOOKUP_MODEL)
    monkeypatch.setattr(server, "_should_learn", lambda *_a: False)
    server._answer_with_history_impl(OFF_QUESTION, [], tier="general")
    record = lanes._TURN.get()
    assert record.bundle.reason is LookupReason.LOW_RELEVANCE and not record.grounded
    assert lanes.receipt_entry(record) == {"applied": False, "reason": "low_relevance",
                                           lanes.RECEIPT_SYSTEM_PROMPT_KEY: False}


def test_lane_on_topic_question_is_framed_with_its_passages(floor_lane, monkeypatch):
    floor_lane.terms = ON_TERMS
    floor_lane.reply = "Event 1102 means the audit log was cleared [1]."
    text, receipt = _http(monkeypatch, ON_QUESTION)

    turn = floor_lane.user_turn()
    assert MARKER_RE.search(turn) and "Reference passages:" in turn and ON_QUESTION in turn
    assert "The audit log was cleared" in turn
    assert "Sources:\n" in text and "[1]* 1102: The audit log was cleared" in text
    assert NONE_LINE not in text
    assert receipt[lanes.RECEIPT_KEY]["applied"] is True
    assert receipt[lanes.RECEIPT_KEY]["passages"] >= 1


def test_lane_other_model_never_looks_anything_up(floor_lane, monkeypatch):
    text = server.sonder(OFF_QUESTION, model_override=PLAIN_MODEL, session="none")
    assert floor_lane.user_turn() == OFF_QUESTION
    assert not floor_lane.expansions()
    assert "Sources:" not in text


# ---------------------------------------------------------------------------
# The multi-model lanes (ensemble_answer and consult)
# ---------------------------------------------------------------------------

@pytest.fixture
def floor_models(models, monkeypatch, path):  # noqa: F811
    monkeypatch.setenv(config.ENV_INDEX, str(path))
    monkeypatch.setattr(state, "_INDEX", None)
    return models


def test_ensemble_unrelated_question_gets_the_bare_prompt_and_one_none_block(
        floor_models, retrievals):  # noqa: F811
    floor_models.replies[MULTI_LOOKUP_MODEL] = "Use rollback netcode."
    floor_models.synth = "Merged: use rollback netcode."
    out = server.ensemble_answer(OFF_QUESTION, tiers="cyber,plain")

    assert len(retrievals) == 1
    bundle = retrievals[0]["bundle"]
    assert bundle.reason is LookupReason.LOW_RELEVANCE and bundle.passages == ()
    members = floor_models.of("member")
    assert len(members) == 2
    assert members[0]["prompt"] == members[1]["prompt"] == OFF_QUESTION
    assert all(not MARKER_RE.search(call["prompt"]) and "Reference passages:" not in call["prompt"]
               for call in floor_models.calls)
    assert out.count("Sources:") == 1
    assert out.count(NONE_LINE) == 1
    assert "Warning:" not in out
    assert "Notes " not in out


def test_consult_unrelated_question_gets_the_bare_prompt_and_one_none_block(floor_models, retrievals):  # noqa: F811
    floor_models.replies[MULTI_LOOKUP_MODEL] = "Use rollback netcode."
    out = server.consult(OFF_QUESTION, tiers="cyber,plain")

    (seen,) = retrievals
    assert seen["bundle"].reason is LookupReason.LOW_RELEVANCE
    members = floor_models.of("member")
    assert members and all(call["prompt"] == OFF_QUESTION for call in members)
    assert out.count(NONE_LINE) == 1 and out.count("Sources:") == 1


def test_ensemble_on_topic_question_keeps_framing_its_members(floor_models, retrievals):  # noqa: F811
    out = server.ensemble_answer(ON_QUESTION, tiers="cyber,plain")

    (seen,) = retrievals
    assert seen["bundle"].applied and seen["bundle"].passages
    members = floor_models.of("member")
    assert all(MARKER_RE.search(call["prompt"]) for call in members)
    assert "Sources:" in out and NONE_LINE not in out


def test_default_consult_path_applies_the_floor_too(floor_models, retrievals):  # noqa: F811
    consult_module.consult(OFF_QUESTION, ["cyber", "plain"])
    assert len(retrievals) == 1
    assert retrievals[0]["bundle"].reason is LookupReason.LOW_RELEVANCE


# ---------------------------------------------------------------------------
# The Sources heading
# ---------------------------------------------------------------------------

def _passages(count=3):
    return tuple(
        Passage(title=f"Title {n}", heading="", source="events", url=f"https://example.test/{n}",
                path=f"{n}.md", text=f"text {n}", rowid=n)
        for n in range(1, count + 1))


def _bundle(passages, warnings=()):
    return lookup.Bundle(applied=True, reason=LookupReason.APPLIED, passages=passages,
                         warnings=tuple(warnings), index_state=IndexState.READY)


NUMBERED = ("  [1]  Title 1 (https://example.test/1)\n"
            "  [2]  Title 2 (https://example.test/2)\n"
            "  [3]  Title 3 (https://example.test/3)")


def test_heading_constants():
    assert lanes.SOURCES_HEAD == "Sources:"
    assert lanes.UNCITED_SOURCES_HEAD == "Sources: retrieved but not cited in the answer"


def test_uncited_reply_gets_the_uncited_heading_over_the_same_list():
    text = lanes.bundle_sources_text(_bundle(_passages()), ())
    assert text.splitlines()[0] == lanes.UNCITED_SOURCES_HEAD
    assert text == lanes.UNCITED_SOURCES_HEAD + "\n" + NUMBERED
    assert lanes.bundle_sources_text(_bundle(_passages()), frozenset()) == text


def test_cited_reply_keeps_the_plain_heading_and_stars_the_passage():
    text = lanes.bundle_sources_text(_bundle(_passages()), {1})
    assert text.splitlines()[0] == "Sources:"
    assert "  [1]* Title 1 (https://example.test/1)" in text
    assert "  [2]  Title 2" in text and "retrieved but not cited" not in text


def test_one_valid_citation_among_invalid_ones_keeps_the_plain_heading():
    text = lanes.bundle_sources_text(_bundle(_passages()), {0, 3, 99})
    assert text.splitlines()[0] == "Sources:"
    assert "  [3]* Title 3" in text


@pytest.mark.parametrize("cited", [{9}, {0}, {4}, {-1}, {4, 9, 0}])
def test_a_citation_out_of_range_does_not_count(cited):
    bundle = _bundle(_passages())
    text = lanes.bundle_sources_text(bundle, cited, [missing_warning(sorted(cited))])
    lines = text.splitlines()
    assert lines[0] == lanes.UNCITED_SOURCES_HEAD
    assert all("*" not in line for line in lines[1:4])
    assert lines[-1] == "Warning: " + missing_warning(sorted(cited))


def test_the_range_boundaries_are_inclusive():
    bundle = _bundle(_passages(3))
    assert lanes.bundle_sources_text(bundle, {1}).splitlines()[0] == "Sources:"
    assert lanes.bundle_sources_text(bundle, {3}).splitlines()[0] == "Sources:"
    assert lanes.bundle_sources_text(bundle, {4}).splitlines()[0] == lanes.UNCITED_SOURCES_HEAD
    assert lanes.bundle_sources_text(_bundle(_passages(1)), {1}).splitlines()[0] == "Sources:"


def test_a_bundle_with_the_bundle_warnings_still_lists_them_after_the_uncited_heading():
    text = lanes.bundle_sources_text(_bundle(_passages(2), ["1 passage(s) left out"]), ())
    lines = text.splitlines()
    assert lines[0] == lanes.UNCITED_SOURCES_HEAD
    assert lines[-1] == "Warning: 1 passage(s) left out"


def test_a_bundle_without_passages_is_unaffected():
    bare = lookup.Bundle(applied=False, reason=LookupReason.NO_MATCH, index_state=IndexState.READY,
                         warnings=(lookup.NO_MATCH_WARNING,))
    assert lanes.bundle_sources_text(bare, ()) == "Sources: none (no reference passages matched the question)"
    empty = lookup.Bundle(applied=True, reason=LookupReason.APPLIED, passages=(),
                          index_state=IndexState.READY)
    text = lanes.bundle_sources_text(empty, ())
    assert text.startswith("Sources: none (") and "retrieved but not cited" not in text
    low = lookup.Bundle(applied=False, reason=LookupReason.LOW_RELEVANCE, index_state=IndexState.READY,
                        warnings=(LOW_RELEVANCE_WARNING,))
    assert lanes.bundle_sources_text(low, {1}) == NONE_LINE
    failed = lookup.Bundle(applied=False, reason=LookupReason.INDEX_UNAVAILABLE,
                           index_state=IndexState.MISSING)
    assert lanes.bundle_sources_text(failed, ()).startswith("Sources: none (reference lookup unavailable")


def test_lane_uncited_reply_gets_the_uncited_heading(floor_lane, monkeypatch):
    floor_lane.terms = ON_TERMS
    floor_lane.reply = "Event 1102 means the audit log was cleared."
    text = _mcp(monkeypatch, ON_QUESTION)

    block = text[text.index("Sources:"):]
    assert block.splitlines()[0] == lanes.UNCITED_SOURCES_HEAD
    assert "[1]  1102: The audit log was cleared" in block and "*" not in block.splitlines()[1]
    assert MARKER_RE.search(floor_lane.user_turn())


def test_lane_cited_reply_keeps_the_plain_heading(floor_lane, monkeypatch):
    floor_lane.terms = ON_TERMS
    floor_lane.reply = "Event 1102 means the audit log was cleared [1]."
    text = _mcp(monkeypatch, ON_QUESTION)

    block = text[text.index("Sources:"):]
    assert block.splitlines()[0] == "Sources:"
    assert "[1]* 1102: The audit log was cleared" in block
    assert "retrieved but not cited" not in text


def test_lane_reply_citing_only_an_out_of_range_number_gets_the_uncited_heading(floor_lane, monkeypatch):
    floor_lane.terms = ON_TERMS
    floor_lane.reply = "Event 1102 means the audit log was cleared [9]."
    text = _mcp(monkeypatch, ON_QUESTION)

    block = text[text.index("Sources:"):]
    assert block.splitlines()[0] == lanes.UNCITED_SOURCES_HEAD
    assert "Warning: " + missing_warning([9]) in block
    assert "*" not in block.split("Warning:")[0]


def test_ensemble_uncited_members_get_the_uncited_heading(floor_models):
    floor_models.replies[MULTI_LOOKUP_MODEL] = "Event 1102 is the audit log being cleared."
    floor_models.synth = "Merged: the audit log was cleared."
    out = server.ensemble_answer(ON_QUESTION, tiers="cyber,plain")
    assert lanes.UNCITED_SOURCES_HEAD in out
    assert out.count("Sources:") == 1
