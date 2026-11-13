"""Reference lookup: the out-of-scope verdict, and a relevance measure that counts single words only.

``split_scope`` reads the RELATED / UNRELATED word a model adds to its search-terms reply;
``retrieve`` raises the relevance floor for a turn whose reply said UNRELATED.  The replies
below are shaped like the ones real models gave.  Coverage values are measured on the corpus
of ``test_reference_lookup_relevance`` (real FTS index, fake model) and asserted before each
case is used, so a case cannot pass by landing in the wrong band.
"""
# ruff: noqa: F811  (fixtures imported from the relevance tests are used as test arguments)
from __future__ import annotations

import pytest

from sonder_runtime.adapters import reference_lookup_service as service
from sonder_runtime.application.chat import reference_lookup as lookup
from sonder_runtime.application.chat.reference_lookup import (
    EXPANSION_FAILED_PREFIX,
    LOW_RELEVANCE_WARNING,
    NO_TERMS_WARNING,
    LookupReason,
    passage_coverage,
)
from sonder_runtime.domain.common.errors import Cancelled
from sonder_runtime.domain.reference_lookup.relevance import (
    MIN_COVERAGE,
    UNRELATED_MIN_COVERAGE,
)
from sonder_runtime.domain.reference_lookup.terms import (
    EXPAND_PROMPT,
    SCOPE_REQUEST,
    build_expand_prompt,
    parse_terms,
    query_terms,
    split_scope,
)
from tests._reference_lookup_index import rule_cache_cleared

# Fixtures and constants of the relevance tests, reused as they are.
from tests.test_reference_lookup_relevance import (  # noqa: F401  (pytest fixtures and helpers)
    BIG_BUDGET,
    MIXED_ON_TERMS,
    MIXED_QUESTION,
    OFF_QUESTION,
    OFF_TERMS,
    configured,
    index,
    path,
)

_forget_rule_cache = pytest.fixture(autouse=True)(rule_cache_cleared)


# ---------------------------------------------------------------------------
# A. split_scope
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reply, unrelated, terms", [
    # the verdict on its own line
    ("4625, logon failure\nUNRELATED", True, ["4625", "logon failure"]),
    ("4625, logon failure\nRELATED", False, ["4625", "logon failure"]),
    # the last comma item of the terms line
    ("Event ID 4697,RELATED", False, ["Event ID 4697"]),
    ("DCSYNC,MITRE,ATT&CK,UNRELATED", True, ["DCSYNC", "MITRE", "ATT&CK"]),
    # after the last term, no comma
    ("OS Credential Dumping LSASS Memory T1003.001 RELATED", False,
     ["OS Credential Dumping LSASS Memory T1003.001"]),
    ("OS Credential Dumping LSASS Memory T1003.001 UNRELATED", True,
     ["OS Credential Dumping LSASS Memory T1003.001"]),
    # labelled lines
    ("First line: a, b\nSecond line: UNRELATED", True, ["a", "b"]),
    ("First line: a, b\nSecond line: RELATED", False, ["a", "b"]),
])
def test_split_scope_reads_the_reply_shapes_models_write(reply, unrelated, terms):
    text, verdict = split_scope(reply)
    assert verdict is unrelated
    assert "RELATED" not in text
    assert parse_terms(text) == terms


@pytest.mark.parametrize("reply", ["", "4625, logon failure", "audit log\n1102"])
def test_split_scope_without_a_verdict_returns_the_text_unchanged(reply):
    assert split_scope(reply) == (reply, False)


def test_split_scope_without_a_verdict_still_strips_line_labels():
    # Observed behaviour, which differs from "no verdict: text unchanged": the "First line:" /
    # "Second line:" labels are removed even when the reply holds no verdict word.
    assert split_scope("First line: a, b\nSecond line:") == ("a, b\n", False)


@pytest.mark.parametrize("reply", [
    "a, b\nUNRELATED\nRELATED",
    "a, b\nRELATED\nUNRELATED",
    "a, RELATED, b, UNRELATED",
    "a, UNRELATED, b, RELATED",
    "UNRELATED RELATED",
    "RELATED UNRELATED",
    # an explanation that names the other word (second review, each read as UNRELATED before)
    "kerberos, tgt\nRELATED (not UNRELATED)",
    "kerberos, tgt\nRELATED, not UNRELATED",
    "kerberos, tgt\nRELATED. It is not UNRELATED.",
    "kerberos, tgt\nRELATED/UNRELATED",
    "kerberos, tgt\nUNRELATED? No: RELATED",
])
def test_split_scope_a_reply_holding_both_verdicts_is_not_unrelated(reply):
    """Taking passages from an in-scope question is the costly mistake, so a mixed reply changes nothing."""
    text, verdict = split_scope(reply)
    assert verdict is False
    assert "RELATED" not in text


@pytest.mark.parametrize("reply, unrelated, terms", [
    ("kerberos, tgt\nVerdict: Related", False, ["kerberos", "tgt"]),
    ("kerberos, tgt\nAnswer: Related", False, ["kerberos", "tgt"]),
    ("pasta, sauce\nanswer = unrelated", True, ["pasta", "sauce"]),
    ("pasta, sauce\nScope: **Unrelated**", True, ["pasta", "sauce"]),
    ("1. lsass, mimikatz\n2. Unrelated", True, ["lsass", "mimikatz"]),
    ("1. lsass, mimikatz\n2) Related.", False, ["lsass", "mimikatz"]),
    ("lsass, Verdict: Unrelated", True, ["lsass"]),
])
def test_split_scope_reads_a_verdict_behind_a_list_number_or_a_label(reply, unrelated, terms):
    text, verdict = split_scope(reply)
    assert verdict is unrelated
    assert parse_terms(text) == terms


@pytest.mark.parametrize("reply, unrelated, terms", [
    ("kerberos, tgt\nRelated - it is about Kerberos", False, ["kerberos", "tgt"]),
    ("pasta, sauce\nUnrelated (cooking)", True, ["pasta", "sauce"]),
    ("pasta, sauce\nUnrelated. The question is about cooking.", True, ["pasta", "sauce"]),
    ("pasta, sauce\nUnrelated: a recipe question", True, ["pasta", "sauce"]),
    ("pasta, sauce\nNot related; it is a recipe", True, ["pasta", "sauce"]),
    ("pasta, sauce\nUnrelated — a recipe question", True, ["pasta", "sauce"]),
    ("pasta, sauce\n2. Verdict: Unrelated (cooking)", True, ["pasta", "sauce"]),
])
def test_split_scope_reads_a_verdict_that_is_followed_by_its_explanation(reply, unrelated, terms):
    """The explanation's words would otherwise be searched for."""
    text, verdict = split_scope(reply)
    assert verdict is unrelated
    assert parse_terms(text) == terms


@pytest.mark.parametrize("reply, terms", [
    ("aes\nrelated-key attack", ["aes", "related-key attack"]),
    ("dns\nunrelated domain", ["dns", "unrelated domain"]),
    ("dns\nrelated.example.com lookup", ["dns", "related.example.com lookup"]),
    ("dns\nrelated events, logon", ["dns", "related events", "logon"]),
    # a line that lists terms is never dropped whole, whatever it opens with
    ("dns\nRelated: zone transfer, axfr", ["dns", "Related: zone transfer", "axfr"]),
    ("dns\nrelated – zone transfer, axfr", ["dns", "related – zone transfer", "axfr"]),
    # the first line is the terms line: it is never dropped whole
    ("Related - kerberos tgt delegation\nRELATED", ["Related - kerberos tgt delegation"]),
])
def test_split_scope_keeps_a_term_that_only_opens_with_the_word(reply, terms):
    text, verdict = split_scope(reply)
    assert verdict is False
    assert parse_terms(text) == terms


@pytest.mark.parametrize("reply", ["UNRELATED", "a, b\nUNRELATED", "a, UNRELATED"])
def test_split_scope_unrelated_is_not_also_counted_as_related(reply):
    # "RELATED" is a substring of "UNRELATED": a reply holding only UNRELATED must still be one verdict.
    text, verdict = split_scope(reply)
    assert verdict is True
    assert "RELATED" not in text


def test_split_scope_a_reply_of_only_the_verdict_leaves_no_terms():
    assert parse_terms(split_scope("UNRELATED")[0]) == []
    assert parse_terms(split_scope("RELATED")[0]) == []


@pytest.mark.parametrize("reply, terms", [
    ("related events, unrelated noise", ["related events", "unrelated noise"]),
    ("logon related, 4625", ["logon related", "4625"]),
    ("Related Events\nUnrelated Noise", ["Related Events", "Unrelated Noise"]),
    ("not related to logon, audit", ["not related to logon", "audit"]),
])
def test_split_scope_the_word_inside_a_term_in_lower_or_title_case_is_an_ordinary_word(reply, terms):
    text, verdict = split_scope(reply)
    assert (text, verdict) == (reply, False)
    assert parse_terms(text) == terms


@pytest.mark.parametrize("reply, unrelated, terms", [
    # found in review: a title-case verdict line became the search term "Unrelated", a word rare
    # enough in the library (7 of 8,908 passages) to count heavily against every passage
    ("recipe, baking\nUnrelated", True, ["recipe", "baking"]),
    ("recipe, baking\nunrelated", True, ["recipe", "baking"]),
    ("4625, logon\nRelated", False, ["4625", "logon"]),
    ("4625, logon\nSecond line: Related", False, ["4625", "logon"]),
    ("4625, logon\nSecond line: Unrelated.", True, ["4625", "logon"]),
    ("4625, logon, related", False, ["4625", "logon"]),
    ("recipe, baking, Unrelated", True, ["recipe", "baking"]),
    ("recipe, baking\n**UNRELATED**", True, ["recipe", "baking"]),
    ("4625, logon\n**Related**", False, ["4625", "logon"]),
    ("recipe, baking\nNOT RELATED", True, ["recipe", "baking"]),
    ("recipe, baking\nNot related.", True, ["recipe", "baking"]),
    ("recipe baking NOT RELATED", True, ["recipe baking"]),
    ("recipe, baking\n- UNRELATED", True, ["recipe", "baking"]),
    ("recipe, baking\n`unrelated`", True, ["recipe", "baking"]),
    ("related", False, []),
    ("unrelated", True, []),
    ("Related, Unrelated", False, []),
])
def test_split_scope_a_line_or_item_that_is_only_the_verdict_counts_in_any_case(reply, unrelated, terms):
    text, verdict = split_scope(reply)
    assert verdict is unrelated
    assert parse_terms(text) == terms
    assert "related" not in text.lower()


def test_split_scope_the_request_copied_back_is_not_a_verdict():
    echo = SCOPE_REQUEST.strip()

    text, verdict = split_scope("4625, logon\n" + echo)
    assert verdict is False
    assert parse_terms(text) == ["4625", "logon"]

    text, verdict = split_scope(echo + "\n4625, logon\nUNRELATED")
    assert verdict is True
    assert parse_terms(text) == ["4625", "logon"]

    second = echo[echo.index("Second line:"):]
    assert split_scope("4625, logon\n" + second) == ("4625, logon\n", False)


@pytest.mark.parametrize("copied", [
    SCOPE_REQUEST.strip().lower(),
    SCOPE_REQUEST.strip().upper(),
    SCOPE_REQUEST[SCOPE_REQUEST.index("RELATED if"):],                    # without its line label
    SCOPE_REQUEST[SCOPE_REQUEST.index("RELATED if"):].replace(" or ", "\nor "),  # wrapped
])
@pytest.mark.parametrize("answer, unrelated", [("", False), ("\nRELATED", False), ("\nUNRELATED", True)])
def test_split_scope_removes_the_request_copied_back_in_any_case_or_without_its_label(copied, answer, unrelated):
    """Second review: the unlabelled copy read as UNRELATED and its words became search terms."""
    text, verdict = split_scope("4625, logon\n" + copied + answer)

    assert verdict is unrelated
    assert parse_terms(text) == ["4625", "logon"]


def test_split_scope_reads_a_long_reply_quickly():
    import time

    reply = ", ".join(["related events"] * 20_000) + "\n" + "*" * 20_000 + "UNRELATED" + "_" * 20_000
    started = time.perf_counter()
    _text, verdict = split_scope(reply)

    assert verdict is True
    assert time.perf_counter() - started < 2.0


@pytest.mark.parametrize("reply", ["UNRELATEDLY", "XRELATED", "RELATED2", "a, UNRELATEDLY", "9RELATED, b",
                                   "XUNRELATED", "UNRELATED7", "UNRELATED_FLAG set, b", "is_RELATED now"])
def test_split_scope_a_verdict_glued_to_other_characters_is_not_a_verdict(reply):
    assert split_scope(reply) == (reply, False)


# ---------------------------------------------------------------------------
# B. build_expand_prompt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question", [
    "What does event 4625 mean?",
    "",
    "a question with {x} and } and { braces",
    "{question}",
])
def test_the_default_prompt_is_the_plain_template(question):
    assert build_expand_prompt(question) == EXPAND_PROMPT.format(question=question)
    assert build_expand_prompt(question, scope=False) == EXPAND_PROMPT.format(question=question)
    assert SCOPE_REQUEST not in build_expand_prompt(question)


@pytest.mark.parametrize("question", [
    "What does event 4625 mean?",
    "",
    "a question with {x} and } and { braces",
    "{question}",
])
def test_the_scoped_prompt_is_the_plain_prompt_plus_the_scope_request(question):
    plain = EXPAND_PROMPT.format(question=question)
    scoped = build_expand_prompt(question, scope=True)
    assert scoped == plain + SCOPE_REQUEST
    assert scoped.startswith(plain)
    assert scoped.endswith(SCOPE_REQUEST)
    assert "RELATED" in SCOPE_REQUEST and "UNRELATED" in SCOPE_REQUEST


def test_a_question_with_braces_does_not_raise_and_appears_verbatim_before_the_scope_text():
    question = "what is {x} and why is there a } here"
    for scope in (False, True):
        prompt = build_expand_prompt(question, scope=scope)
        assert question in prompt
    scoped = build_expand_prompt(question, scope=True)
    assert scoped.index(question) < scoped.index(SCOPE_REQUEST)


def test_the_scope_flag_is_keyword_only():
    with pytest.raises(TypeError):
        build_expand_prompt("q", True)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# F. the two floors
# ---------------------------------------------------------------------------

def test_the_unrelated_floor_is_above_the_plain_floor_which_is_above_zero():
    assert UNRELATED_MIN_COVERAGE > MIN_COVERAGE > 0
    assert MIN_COVERAGE == 0.20
    assert UNRELATED_MIN_COVERAGE == 0.40


# ---------------------------------------------------------------------------
# C. retrieve
# ---------------------------------------------------------------------------

# (question, the terms line a model gives).  Coverage of the best passage, measured below:
# LOW under 0.20, MID from 0.20 up to 0.40, HIGH from 0.40 up.
CASES = {
    "low": (OFF_QUESTION, OFF_TERMS),
    "mid": (MIXED_QUESTION, "server time"),
    "high": (MIXED_QUESTION, MIXED_ON_TERMS),
}


def _expand_with(reply, prompts=None):
    def expand(prompt):
        if prompts is not None:
            prompts.append(prompt)
        if isinstance(reply, BaseException):
            raise reply
        return reply
    return expand


def _coverage(idx, question, bundle):
    return passage_coverage(idx, query_terms(question, bundle.terms), bundle.passages)


def _band_coverage(idx, band):
    """The best-passage coverage of a case, measured on the plain search, and its band asserted."""
    question, reply = CASES[band]
    plain = lookup.retrieve(question, _expand_with(reply), 5, BIG_BUDGET, index=idx)
    assert plain.applied and plain.passages
    covered = _coverage(idx, question, plain)
    assert covered is not None
    if band == "low":
        assert covered < MIN_COVERAGE
    elif band == "mid":
        assert MIN_COVERAGE < covered < UNRELATED_MIN_COVERAGE
    else:
        assert covered >= UNRELATED_MIN_COVERAGE
    return covered


def _scoped(idx, band, verdict=None, *, min_coverage=MIN_COVERAGE, unrelated_coverage=UNRELATED_MIN_COVERAGE,
            prompts=None, joiner="\n"):
    question, reply = CASES[band]
    if verdict:
        reply = reply + joiner + verdict
    return lookup.retrieve(question, _expand_with(reply, prompts), 5, BIG_BUDGET, index=idx,
                           min_coverage=min_coverage, unrelated_coverage=unrelated_coverage)


def test_the_corpus_bands_hold(index):
    low, mid, high = (_band_coverage(index, band) for band in ("low", "mid", "high"))
    assert low < MIN_COVERAGE < mid < UNRELATED_MIN_COVERAGE <= high


@pytest.mark.parametrize("min_coverage, unrelated_coverage, scoped", [
    (MIN_COVERAGE, UNRELATED_MIN_COVERAGE, True),
    (0.01, 0.02, True),
    (0.0, UNRELATED_MIN_COVERAGE, False),
    (-0.1, UNRELATED_MIN_COVERAGE, False),
    (MIN_COVERAGE, 0.0, False),
    (MIN_COVERAGE, MIN_COVERAGE, False),
    (UNRELATED_MIN_COVERAGE, MIN_COVERAGE, False),
    (UNRELATED_MIN_COVERAGE, UNRELATED_MIN_COVERAGE, False),
    (0.0, 0.0, False),
])
def test_the_scope_line_is_requested_only_when_the_unrelated_floor_is_above_a_positive_floor(
        index, min_coverage, unrelated_coverage, scoped):
    prompts = []
    _scoped(index, "mid", min_coverage=min_coverage, unrelated_coverage=unrelated_coverage, prompts=prompts)
    question = CASES["mid"][0]
    assert prompts == [build_expand_prompt(question, scope=scoped)]
    assert (SCOPE_REQUEST in prompts[0]) is scoped


def test_the_default_call_is_a_plain_search_with_the_plain_prompt(index):
    prompts = []
    question, reply = CASES["mid"]
    lookup.retrieve(question, _expand_with(reply, prompts), 5, BIG_BUDGET, index=index)
    assert prompts == [build_expand_prompt(question)]


@pytest.mark.parametrize("verdict", ["UNRELATED", "RELATED", None])
def test_without_a_floor_no_verdict_removes_anything(index, verdict):
    for band in ("low", "mid", "high"):
        bundle = _scoped(index, band, verdict, min_coverage=0.0, unrelated_coverage=0.0)
        assert bundle.applied and bundle.reason is LookupReason.APPLIED


def test_unrelated_with_middling_coverage_raises_the_floor_and_drops_the_passages(index):
    covered = _band_coverage(index, "mid")
    bundle = _scoped(index, "mid", "UNRELATED")
    assert not bundle.applied
    assert bundle.reason is LookupReason.LOW_RELEVANCE
    assert bundle.passages == ()
    assert LOW_RELEVANCE_WARNING in bundle.warnings
    assert f"best passage coverage {covered:.2f} is under {UNRELATED_MIN_COVERAGE:.2f}" in bundle.detail
    assert "0.40" in bundle.detail
    assert "judged the question unrelated" in bundle.detail
    assert "RELATED" not in " ".join(bundle.terms)


@pytest.mark.parametrize("joiner", ["\n", ", ", " "])
def test_the_verdict_is_read_wherever_the_model_put_it(index, joiner):
    bundle = _scoped(index, "mid", "UNRELATED", joiner=joiner)
    assert bundle.reason is LookupReason.LOW_RELEVANCE
    assert "judged the question unrelated" in bundle.detail
    assert list(bundle.terms) == ["server time"]


def test_the_raised_floor_is_a_strict_less_than(index):
    covered = _band_coverage(index, "mid")
    assert _scoped(index, "mid", "UNRELATED", unrelated_coverage=covered).applied
    assert not _scoped(index, "mid", "UNRELATED", unrelated_coverage=covered + 1e-6).applied


def test_unrelated_with_high_coverage_never_removes_the_passages(index):
    _band_coverage(index, "high")
    bundle = _scoped(index, "high", "UNRELATED")
    assert bundle.applied and bundle.reason is LookupReason.APPLIED
    assert bundle.passages
    assert LOW_RELEVANCE_WARNING not in bundle.warnings
    assert bundle.passages == _scoped(index, "high", "RELATED").passages


@pytest.mark.parametrize("verdict", ["RELATED", None])
def test_related_or_no_verdict_with_middling_coverage_keeps_the_passages(index, verdict):
    _band_coverage(index, "mid")
    bundle = _scoped(index, "mid", verdict)
    assert bundle.applied and bundle.reason is LookupReason.APPLIED
    assert bundle.passages
    assert LOW_RELEVANCE_WARNING not in bundle.warnings
    assert list(bundle.terms) == ["server time"]


@pytest.mark.parametrize("verdict, floor_text, judged", [
    ("RELATED", "0.20", False),
    (None, "0.20", False),
    ("UNRELATED", "0.40", True),
])
def test_low_coverage_is_low_relevance_under_the_floor_the_verdict_selects(index, verdict, floor_text, judged):
    covered = _band_coverage(index, "low")
    bundle = _scoped(index, "low", verdict)
    assert not bundle.applied and bundle.reason is LookupReason.LOW_RELEVANCE
    assert bundle.passages == ()
    assert LOW_RELEVANCE_WARNING in bundle.warnings
    assert bundle.detail.startswith(f"best passage coverage {covered:.2f} is under {floor_text}")
    assert ("judged the question unrelated" in bundle.detail) is judged


def test_a_failed_search_terms_call_leaves_the_floor_at_the_plain_one(index):
    _band_coverage(index, "mid")
    prompts = []
    bundle = lookup.retrieve(CASES["mid"][0], _expand_with(RuntimeError("model gone"), prompts), 5,
                             BIG_BUDGET, index=index, min_coverage=MIN_COVERAGE,
                             unrelated_coverage=UNRELATED_MIN_COVERAGE)
    assert prompts == [build_expand_prompt(CASES["mid"][0], scope=True)]   # the scoped request was made
    assert any(w.startswith(EXPANSION_FAILED_PREFIX) for w in bundle.warnings)
    assert bundle.applied and bundle.passages
    assert LOW_RELEVANCE_WARNING not in bundle.warnings


def test_a_cancelled_search_terms_call_is_still_re_raised_when_scoped(index):
    with pytest.raises(Cancelled):
        lookup.retrieve(CASES["mid"][0], _expand_with(Cancelled("the caller cancelled the turn")), 5,
                        BIG_BUDGET, index=index, min_coverage=MIN_COVERAGE,
                        unrelated_coverage=UNRELATED_MIN_COVERAGE)


def test_without_an_expand_function_the_floor_stays_at_the_plain_one(index):
    question = CASES["mid"][0]
    bundle = lookup.retrieve(question, None, 5, BIG_BUDGET, index=index, min_coverage=MIN_COVERAGE,
                             unrelated_coverage=UNRELATED_MIN_COVERAGE)
    covered = _coverage(index, question, bundle)
    assert MIN_COVERAGE < covered < UNRELATED_MIN_COVERAGE
    assert bundle.applied and bundle.passages
    assert bundle == lookup.retrieve(question, None, 5, BIG_BUDGET, index=index, min_coverage=MIN_COVERAGE)


def test_a_reply_of_only_unrelated_still_raises_the_floor_and_warns_of_no_terms(index):
    question = CASES["mid"][0]
    bundle = lookup.retrieve(question, _expand_with("UNRELATED"), 5, BIG_BUDGET, index=index,
                             min_coverage=MIN_COVERAGE, unrelated_coverage=UNRELATED_MIN_COVERAGE)
    plain = lookup.retrieve(question, None, 5, BIG_BUDGET, index=index)
    assert MIN_COVERAGE < _coverage(index, question, plain) < UNRELATED_MIN_COVERAGE
    assert NO_TERMS_WARNING in bundle.warnings
    assert LOW_RELEVANCE_WARNING in bundle.warnings
    assert bundle.reason is LookupReason.LOW_RELEVANCE and not bundle.applied
    assert "judged the question unrelated" in bundle.detail


def test_a_reply_of_only_related_warns_of_no_terms_and_keeps_the_passages(index):
    bundle = lookup.retrieve(CASES["mid"][0], _expand_with("RELATED"), 5, BIG_BUDGET, index=index,
                             min_coverage=MIN_COVERAGE, unrelated_coverage=UNRELATED_MIN_COVERAGE)
    assert NO_TERMS_WARNING in bundle.warnings
    assert bundle.applied and bundle.passages


@pytest.mark.parametrize("band", ["low", "mid", "high"])
@pytest.mark.parametrize("verdict, joiner", [
    ("UNRELATED", "\n"), ("RELATED", "\n"), ("UNRELATED", ", "), ("RELATED", ", "), ("RELATED", " "),
])
def test_the_verdict_word_is_never_a_search_term_when_scoped(index, band, verdict, joiner):
    bundle = _scoped(index, band, verdict, joiner=joiner)
    assert bundle.terms
    for term in bundle.terms:
        assert "RELATED" not in term


def test_a_plain_search_parses_a_reply_ending_related_as_before(index):
    # Not scoped: the reply is not split, so the word is one more search term, exactly as it was.
    question, reply = CASES["mid"]
    for kwargs in ({}, {"min_coverage": MIN_COVERAGE}, {"min_coverage": MIN_COVERAGE, "unrelated_coverage": 0.0},
                   {"unrelated_coverage": UNRELATED_MIN_COVERAGE}):
        bundle = lookup.retrieve(question, _expand_with(reply + ", RELATED"), 5, BIG_BUDGET, index=index,
                                 **kwargs)
        assert list(bundle.terms) == ["server time", "RELATED"], kwargs
    bundle = lookup.retrieve(question, _expand_with(reply + "\nUNRELATED"), 5, BIG_BUDGET, index=index,
                             min_coverage=MIN_COVERAGE)
    assert list(bundle.terms) == ["server time", "UNRELATED"]
    assert bundle.applied      # and an UNRELATED word there never lifts the floor


def test_a_reply_that_is_not_text_is_not_split(index):
    # The split is only for a string reply; any other reply goes to the parser as before.
    question = CASES["mid"][0]
    bundle = lookup.retrieve(question, lambda prompt: None, 5, BIG_BUDGET, index=index,
                             min_coverage=MIN_COVERAGE, unrelated_coverage=UNRELATED_MIN_COVERAGE)
    assert any(w.startswith(EXPANSION_FAILED_PREFIX) for w in bundle.warnings)
    assert bundle.applied


# ---------------------------------------------------------------------------
# D. passage_coverage measures single words only
# ---------------------------------------------------------------------------

def _best_passages(idx):
    bundle = lookup.retrieve(CASES["mid"][0], _expand_with(CASES["mid"][1]), 5, BIG_BUDGET, index=idx)
    assert bundle.passages
    return bundle.passages


@pytest.mark.parametrize("phrase", ["zzz qqq", "rollback netcode", "a b c d", " lead", "audit  log"])
def test_a_phrase_no_passage_holds_does_not_change_the_coverage(index, phrase):
    passages = _best_passages(index)
    words = ["server", "kerberos", "time"]
    base = passage_coverage(index, words, passages)
    assert base is not None and 0 < base < 1
    assert passage_coverage(index, words + [phrase], passages) == base
    assert passage_coverage(index, [phrase] + words, passages) == base


def test_phrases_are_never_asked_of_the_index(index):
    seen = []
    real = index.term_stats

    class Spy:
        def term_stats(self, terms, rowids):
            seen.append(list(terms))
            return real(terms, rowids)

    passages = _best_passages(index)
    passage_coverage(Spy(), ["server", "server time", "client"], passages)
    assert seen == [["server", "client"]]


@pytest.mark.parametrize("terms", [["server time"], ["audit log", "kerberos ticket", "zzz qqq"], ["a b"]])
def test_a_terms_list_of_phrases_only_is_not_measurable(index, terms):
    assert passage_coverage(index, terms, _best_passages(index)) is None


def test_non_string_terms_are_ignored_without_raising(index):
    passages = _best_passages(index)
    base = passage_coverage(index, ["server", "time"], passages)
    assert base is not None
    assert passage_coverage(index, ["server", None, 5, b"time", ("a",), "time", 2.5], passages) == base
    assert passage_coverage(index, [None, 5, b"x"], passages) is None


# ---------------------------------------------------------------------------
# E. the service
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("for_answer, expected_min, expected_unrelated", [
    (True, MIN_COVERAGE, UNRELATED_MIN_COVERAGE),
    (False, 0.0, 0.0),
])
def test_retrieve_for_passes_the_floors_by_purpose(configured, monkeypatch, for_answer, expected_min,
                                                   expected_unrelated):
    seen = []
    real = lookup.retrieve

    def spy(*args, **kwargs):
        seen.append((kwargs.get("min_coverage"), kwargs.get("unrelated_coverage")))
        return real(*args, **kwargs)

    monkeypatch.setattr(lookup, "retrieve", spy)
    service.retrieve_for(MIXED_QUESTION, None, 5, BIG_BUDGET, for_answer=for_answer)
    assert seen == [(expected_min, expected_unrelated)]


def test_retrieve_for_the_default_is_the_plain_search(configured, monkeypatch):
    seen = []
    real = lookup.retrieve
    monkeypatch.setattr(lookup, "retrieve", lambda *a, **kw: (seen.append(kw), real(*a, **kw))[1])
    service.retrieve_for(MIXED_QUESTION, None, 5, BIG_BUDGET)
    assert seen[0]["min_coverage"] == 0.0 and seen[0]["unrelated_coverage"] == 0.0


def test_the_scope_line_reaches_the_model_only_for_an_answer(configured):
    answer_prompts, plain_prompts = [], []
    question, reply = CASES["mid"]
    answer = service.retrieve_for(question, _expand_with(reply + "\nUNRELATED", answer_prompts), 5,
                                  BIG_BUDGET, for_answer=True)
    plain = service.retrieve_for(question, _expand_with(reply + "\nUNRELATED", plain_prompts), 5,
                                 BIG_BUDGET, for_answer=False)
    assert answer_prompts == [build_expand_prompt(question, scope=True)]
    assert plain_prompts == [build_expand_prompt(question)]
    # An UNRELATED verdict with middling coverage costs the answer its passages ...
    assert answer.reason is LookupReason.LOW_RELEVANCE and answer.passages == ()
    # ... and the plain search tool never splits the reply, so its word is a term and nothing is dropped.
    assert plain.applied and plain.passages
    assert "UNRELATED" in plain.terms


def test_an_answer_with_a_related_verdict_keeps_its_passages(configured):
    question, reply = CASES["mid"]
    answer = service.retrieve_for(question, _expand_with(reply + "\nRELATED"), 5, BIG_BUDGET,
                                  for_answer=True)
    assert answer.applied and answer.passages
    assert list(answer.terms) == ["server time"]
