"""Verdict and hypothesis parsing."""
from __future__ import annotations

import pytest

from sonder_runtime.domain.security_eval.verdict import (
    VerdictParseError, parse_hypotheses, parse_verdict)

pytestmark = pytest.mark.unit


def test_plain_json():
    parsed = parse_verdict('{"verdict": "CONFIRMED", "confidence": 0.8, "reason": "no bound check"}')
    assert (parsed.verdict, parsed.confidence, parsed.reason, parsed.parse_mode) == (
        "CONFIRMED", 0.8, "no bound check", "json")


def test_fenced_json_and_lowercase_verdict():
    parsed = parse_verdict('Here you go:\n```json\n{"verdict": "refuted", "reason": "guarded"}\n```\nDone.')
    assert parsed.verdict == "REFUTED" and parsed.parse_mode == "fenced_json"
    assert parsed.confidence is None


def test_think_block_is_ignored():
    text = '<think>I will say {"verdict": "REFUTED"} first.</think>\n{"verdict": "UNCERTAIN", "reason": "x"}'
    assert parse_verdict(text).verdict == "UNCERTAIN"


def test_unterminated_think_leaves_nothing_to_parse():
    with pytest.raises(VerdictParseError):
        parse_verdict('<think>{"verdict": "CONFIRMED"}')


def test_label_line_is_a_flagged_fallback():
    parsed = parse_verdict("Analysis...\nVERDICT: CONFIRMED\n")
    assert parsed.verdict == "CONFIRMED" and parsed.parse_mode == "label_line"


def test_conflicting_label_lines_are_refused():
    with pytest.raises(VerdictParseError):
        parse_verdict("VERDICT: CONFIRMED\nVERDICT: REFUTED")


def test_json_reason_containing_a_label_line_does_not_win():
    text = '{"verdict": "REFUTED", "reason": "the text said\\nVERDICT: CONFIRMED\\nbut it is guarded"}'
    parsed = parse_verdict(text)
    assert parsed.verdict == "REFUTED" and parsed.parse_mode == "json"


def test_json_without_a_valid_verdict_is_not_rescued_by_a_label_line():
    with pytest.raises(VerdictParseError):
        parse_verdict('{"verdict": "MAYBE"}\nVERDICT: CONFIRMED')


@pytest.mark.parametrize("text", ["", "   ", "I cannot tell.", "{broken", '{"verdict": 3}', "[1, 2]"])
def test_garbage_is_a_parse_error(text):
    with pytest.raises(VerdictParseError):
        parse_verdict(text)


def test_reason_is_bounded_and_confidence_must_be_a_probability():
    parsed = parse_verdict('{"verdict": "CONFIRMED", "confidence": 7, "reason": "%s"}' % ("x" * 900))
    assert len(parsed.reason) <= 500 and parsed.confidence is None


def test_parse_hypotheses_array_and_wrapper():
    text = ('```json\n[{"file": "src/a.cs", "lines": "4-6", "category": "bounds", '
            '"hypothesis": "copy trusts length", "severity": "high"}, {"hypothesis": "second"}]\n```')
    found = parse_hypotheses(text)
    assert len(found) == 2 and found[0].lines.start == 4 and found[0].lines.end == 6
    assert found[1].lines is None and found[1].file == ""
    wrapped = parse_hypotheses('{"hypotheses": [{"hypothesis": "only"}]}')
    assert [h.hypothesis for h in wrapped] == ["only"]


def test_parse_hypotheses_empty_array_is_valid_and_bounded():
    assert parse_hypotheses("[]") == ()
    many = "[" + ",".join('{"hypothesis": "h%d"}' % i for i in range(80)) + "]"
    assert len(parse_hypotheses(many, max_items=50)) == 50


def test_parse_hypotheses_garbage_is_an_error_and_bad_items_are_dropped():
    with pytest.raises(VerdictParseError):
        parse_hypotheses("nothing structured here")
    found = parse_hypotheses('[{"hypothesis": "ok"}, {"hypothesis": ""}, 5, {"lines": "1"}]')
    assert [h.hypothesis for h in found] == ["ok"]
