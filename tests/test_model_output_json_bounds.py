"""Depth bounds preserve tolerant JSON parsing without decoding hostile nesting."""
import json

import pytest

from sonder_runtime.domain.computer_use import rules
from sonder_runtime.domain.security_eval import verdict

pytestmark = pytest.mark.unit


def _arrays(count):
    return "[" * count + "0" + "]" * count


@pytest.mark.parametrize("depth", [64, 65])
def test_model_step_depth_boundary(depth):
    # One object plus depth-1 arrays exercises the agreed 64-container limit.
    text = '{"done":true,"ignored":' + _arrays(depth - 1) + "}"
    if depth == 64:
        assert rules.parse_model_step(text)["done"] is True
    else:
        with pytest.raises(rules.ActionRefused):
            rules.parse_model_step(text)


@pytest.mark.parametrize("parser", ["step", "verdict", "hypotheses"])
def test_quoted_and_escaped_brackets_are_not_nesting(parser):
    # json.dumps produces valid JSON, including a literal backslash and quote.
    literal = "[{" * 40 + '\\"' + "}]" * 40
    if parser == "step":
        result = rules.parse_model_step(json.dumps({"done": True, "reason": literal}))
        assert result["reason"] == literal
    elif parser == "verdict":
        result = verdict.parse_verdict(json.dumps({"verdict": "REFUTED", "reason": literal}))
        assert result.reason == literal
    else:
        result = verdict.parse_hypotheses(json.dumps([{"hypothesis": literal}]))
        assert [item.hypothesis for item in result] == [literal]


@pytest.mark.parametrize("parser", ["verdict", "hypotheses"])
def test_valid_candidate_is_not_rejected_by_trailing_deep_junk(parser):
    suffix = "\n" + "[" * 3000
    if parser == "verdict":
        result = verdict.parse_verdict('{"verdict":"REFUTED"}' + suffix)
        assert result.verdict == "REFUTED"
        assert result.parse_mode == "json"
    else:
        result = verdict.parse_hypotheses('[{"hypothesis":"kept"}]' + suffix)
        assert [item.hypothesis for item in result] == ["kept"]


@pytest.mark.parametrize("parser", ["verdict", "hypotheses"])
def test_deep_ignored_fields_do_not_bypass_the_depth_bound(parser):
    if parser == "verdict":
        text = '{"verdict":"CONFIRMED","ignored":' + _arrays(64) + "}"
        parse = verdict.parse_verdict
    else:
        text = '[{"hypothesis":"kept","ignored":' + _arrays(63) + "}]"
        parse = verdict.parse_hypotheses
    with pytest.raises(verdict.VerdictParseError):
        parse(text)


@pytest.mark.parametrize("parser", ["verdict", "hypotheses"])
def test_overdeep_candidates_are_rejected_before_decoder_work(monkeypatch, parser):
    calls = []
    decoder = json.JSONDecoder()

    class RecordingDecoder:
        def raw_decode(self, text, index):
            calls.append(index)
            return decoder.raw_decode(text, index)

    monkeypatch.setattr(verdict, "_DECODER", RecordingDecoder())
    if parser == "verdict":
        text = '{"a":' * 3000
        parse = verdict.parse_verdict
    else:
        text = "[" * 30_000
        parse = verdict.parse_hypotheses
    with pytest.raises(verdict.VerdictParseError):
        parse(text)
    # A wall-clock threshold cannot prove that decoder allocations were avoided.
    assert calls == []


@pytest.mark.parametrize("parser", ["verdict", "hypotheses"])
def test_tolerant_recovery_survives_the_depth_guard(parser):
    if parser == "verdict":
        # The outer object is truncated; its complete inner object remains usable.
        text = 'int f() { count++; }\n{"result":{"verdict":"REFUTED"}'
        result = verdict.parse_verdict(text)
        assert result.verdict == "REFUTED"
    else:
        # Implausible prose brackets must not spend the failed-decode allowance.
        result = verdict.parse_hypotheses("[i] " * 200 + '[{"hypothesis":"kept"}]')
        assert [item.hypothesis for item in result] == ["kept"]


@pytest.mark.parametrize("text", [
    '{"verdict":"REFUTED","ignored":' + _arrays(64) + '}\nVERDICT: CONFIRMED',
    'VERDICT: CONFIRMED\n' + '{"x":' * 64 + 'junk\n{"verdict":"REFUTED"}',
], ids=["depth-refused", "failure-cap"])
def test_incomplete_json_scan_never_promotes_conflicting_label(text):
    try:
        result = verdict.parse_verdict(text)
    except verdict.VerdictParseError:
        # Refusal is safe when bounded scanning cannot establish JSON precedence.
        return
    assert (result.verdict, result.parse_mode) == ("REFUTED", "json")


@pytest.mark.parametrize("parser", ["verdict", "hypotheses"])
def test_earlier_valid_json_wins_before_bounded_junk_and_conflicting_label(parser):
    suffix = "\n" + "[" * 3000 + "\nVERDICT: CONFIRMED"
    if parser == "verdict":
        result = verdict.parse_verdict('{"verdict":"REFUTED"}' + suffix)
        assert (result.verdict, result.parse_mode) == ("REFUTED", "json")
    else:
        result = verdict.parse_hypotheses('[{"hypothesis":"trusted"}]' + suffix)
        assert [item.hypothesis for item in result] == ["trusted"]


@pytest.mark.parametrize("text", [
    '{"verdict":"REFUTED","x":' + _arrays(64) + '}\n{"verdict":"CONFIRMED"}',
    '{"x":' + "[" * 64 + '{"verdict":"CONFIRMED"}' + "]" * 64 + ',"verdict":"REFUTED"}',
], ids=["later-object", "nested-object"])
def test_depth_refusal_never_promotes_a_later_or_nested_verdict(text):
    # An unbounded decoder reads REFUTED from both replies. The bounded scan cannot decode the
    # refused container, so it must refuse rather than return the CONFIRMED that follows it or
    # sits inside it.
    with pytest.raises(verdict.VerdictParseError):
        verdict.parse_verdict(text)


@pytest.mark.parametrize("later", [
    '[{"hypothesis":"b"}]',
    '{"hypotheses":[{"hypothesis":"b"}]}',
], ids=["later-array", "later-object"])
def test_depth_refusal_never_promotes_a_later_hypothesis_list(later):
    # An unbounded decoder returns hypothesis "a" from the refused first list.
    text = '[{"hypothesis":"a","x":' + _arrays(63) + "}]\n" + later
    with pytest.raises(verdict.VerdictParseError):
        verdict.parse_hypotheses(text)


@pytest.mark.parametrize("parser", ["verdict", "hypotheses"])
def test_values_before_a_depth_refusal_keep_their_precedence(parser):
    if parser == "verdict":
        text = '{"verdict":"REFUTED"}\n{"x":' + _arrays(64) + '}\n{"verdict":"CONFIRMED"}'
        result = verdict.parse_verdict(text)
        assert (result.verdict, result.parse_mode) == ("REFUTED", "json")
    else:
        text = '[{"hypothesis":"a"}]\n[' + _arrays(64) + ']\n[{"hypothesis":"b"}]'
        assert [item.hypothesis for item in verdict.parse_hypotheses(text)] == ["a"]


# Openers that pass the plausibility filter but do not decode, each at O(1) cost: line-range
# citations and quoted dict literals, as security-analysis replies carry them.
_CITATIONS = "".join("buf overflow at lines [%d-%d] in foo.c\n" % (n, n + 3) for n in range(1, 4000, 4))
_DICT_LITERALS = "".join('cfg = {"k%d": os.environ}\n' % n for n in range(1000))


def test_many_cheap_failed_openers_do_not_refuse_a_hypotheses_reply():
    result = verdict.parse_hypotheses(_CITATIONS + '[{"hypothesis":"kept","file":"foo.c"}]')
    assert [(item.hypothesis, item.file) for item in result] == [("kept", "foo.c")]


@pytest.mark.parametrize("answer, expected", [
    ('{"verdict":"REFUTED"}', ("REFUTED", "json")),
    ("VERDICT: REFUTED", ("REFUTED", "label_line")),
], ids=["json", "label-line"])
def test_many_cheap_failed_openers_do_not_refuse_a_verdict_reply(answer, expected):
    result = verdict.parse_verdict(_DICT_LITERALS + answer)
    assert (result.verdict, result.parse_mode) == expected


@pytest.mark.parametrize("parser, text", [
    # Nested unclosed containers inside the depth bound: every opener walks to the end of the text.
    ("hypotheses", "[" * 60 + "[]" * 99_000),
    ("verdict", '{"a":' * 63 + "{" + '"a":0,' * 33_000),
    ("hypotheses", '{"hypotheses":' * 63 + "{" + '"a":0,' * 33_000),
], ids=["arrays", "objects", "hypotheses-objects"])
def test_failed_decode_work_stays_linear(monkeypatch, parser, text):
    walked = []
    real = verdict.container_extent

    def recording(value, start, *args, **kwargs):
        deepest, end = real(value, start, *args, **kwargs)
        walked.append(end - start)
        return deepest, end

    monkeypatch.setattr(verdict, "container_extent", recording)
    parse = verdict.parse_verdict if parser == "verdict" else verdict.parse_hypotheses
    with pytest.raises(verdict.VerdictParseError):
        parse(text)
    # Each of the (at most two) scans may walk its failure budget, one attempt past it, and the
    # disjoint successful values once.
    per_scan = verdict._FAILED_SCAN_FACTOR * len(text) + verdict._FAILED_SCAN_FLOOR + 2 * len(text)
    assert sum(walked) <= 2 * per_scan, (sum(walked), len(text))
