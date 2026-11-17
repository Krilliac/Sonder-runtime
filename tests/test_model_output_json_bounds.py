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
