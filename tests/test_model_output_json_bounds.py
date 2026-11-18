"""Depth bounds preserve tolerant JSON parsing without decoding hostile nesting."""
import json
import random

import pytest

from sonder_runtime.domain.common.bounded_json import container_extent, container_extents, container_walk
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
        texts = ['{"a":' * 3000, '{"a":' * 3000 + "0" + "}" * 3000]
        parse = verdict.parse_verdict
    else:
        texts = ["[" * 30_000, _arrays(30_000)]
        parse = verdict.parse_hypotheses
    for text in texts:  # never closed, then closed
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


def _record_work(monkeypatch):
    """Count, without asking the code under test, the characters each scan handed the decoder and
    the characters its extent passes covered.

    ``handed`` gets the length of every string passed to ``raw_decode`` (the decoder never reads
    past it, so it bounds both the copy and the read); ``passes`` gets ``len(text)`` for every
    ``container_extents`` table and ``end - start`` for every ``container_walk``.
    """
    handed, passes = [], []
    decoder = json.JSONDecoder()
    real_extents, real_walk = verdict.container_extents, verdict.container_walk

    class RecordingDecoder:
        def raw_decode(self, piece, index):
            handed.append(len(piece) - index)
            return decoder.raw_decode(piece, index)

    def extents(text):
        passes.append(len(text))
        return real_extents(text)

    def walk(text, start, *args, **kwargs):
        deepest, end, closed = real_walk(text, start, *args, **kwargs)
        passes.append(end - start)
        return deepest, end, closed

    monkeypatch.setattr(verdict, "_DECODER", RecordingDecoder())
    monkeypatch.setattr(verdict, "container_extents", extents)
    monkeypatch.setattr(verdict, "container_walk", walk)
    return handed, passes


@pytest.mark.parametrize("parser, text", [
    # Nested closed containers inside the depth bound, each failing at the far end: every
    # opener's decode reads almost the whole reply.
    ("hypotheses", "[" * 60 + "0," * 99_000 + "x" + "]" * 60),
    ("verdict", '{"a":' * 63 + "{" + '"a":0,' * 33_000 + '"a":x' + "}" * 64),
    ("hypotheses", '{"hypotheses":' * 63 + "{" + '"a":0,' * 33_000 + '"a":x' + "}" * 64),
], ids=["arrays", "objects", "hypotheses-objects"])
def test_failed_decode_work_stays_linear(monkeypatch, parser, text):
    handed, passes = _record_work(monkeypatch)
    parse = verdict.parse_verdict if parser == "verdict" else verdict.parse_hypotheses
    with pytest.raises(verdict.VerdictParseError, match="work limit"):
        parse(text)
    # Here every failed decode reads (nearly) all it is handed, so what was handed is the read.
    # Each of the (at most two) scans may read its failure budget plus one attempt past it, and
    # the extent passes cover the reply a few times.
    per_scan = verdict._FAILED_SCAN_FACTOR * len(text) + verdict._FAILED_SCAN_FLOOR + len(text)
    assert sum(handed) <= 2 * per_scan, (sum(handed), len(text))
    assert sum(passes) <= 4 * len(text), (sum(passes), len(text))
    # The budget, not the end of the openers, stopped the scan.
    assert len(handed) < 20 < text.count(text[0]), (len(handed), len(text))


def test_failed_decode_copies_stay_linear(monkeypatch):
    # Openers inside one long string share its continuation: each fails after a few characters,
    # but its extent (the copy it is decoded from) runs to the closing bracket at the end, so
    # uncharged copies would cost about n * n / 12 characters.
    text = '"' + ("[0 x\\\"" * 10_000) + '"]'
    handed, passes = _record_work(monkeypatch)
    with pytest.raises(verdict.VerdictParseError):
        verdict.parse_hypotheses(text)
    budget = verdict._FAILED_SCAN_FACTOR * len(text) + verdict._FAILED_SCAN_FLOOR
    # Each failed decode is charged at least a _COPY_DIVISOR-th of its copy, less rounding.
    copies = verdict._COPY_DIVISOR * (budget + len(text)) + verdict._COPY_DIVISOR * len(handed)
    assert sum(handed) <= 2 * copies, (sum(handed), len(text))
    assert sum(handed) < len(text) * len(text) // 40, (sum(handed), len(text))
    assert sum(passes) <= 4 * len(text), (sum(passes), len(text))


def _decode_error(piece):
    try:
        json.JSONDecoder().raw_decode(piece, 0)
    except json.JSONDecodeError as error:
        return error
    raise AssertionError(piece)


def test_an_unterminated_string_is_charged_the_whole_piece():
    # The decoder reports an unterminated string where it starts, after reading to the end.
    piece = '["' + "a" * 5_000
    error = _decode_error(piece)
    assert error.msg.startswith("Unterminated string") and error.pos == 1
    assert verdict._failed_decode_cost(piece, error) >= len(piece)


@pytest.mark.parametrize("piece", [
    '["x" "y", ' + '"pad", ' * 2_000 + '"z"]',
    '{"notes" "x", "pad": [' + '"pad", ' * 2_000 + '"z"]}',
], ids=["missing-comma", "missing-colon"])
def test_an_error_at_a_quote_is_charged_where_the_decoder_stopped(piece):
    # A missing comma or colon between strings is reported at the next quote, a few characters
    # in: the decoder stopped there, so it is not charged the rest of the piece.
    error = _decode_error(piece)
    assert piece.startswith('"', error.pos) and error.pos < 20
    assert verdict._failed_decode_cost(piece, error) <= 20 + len(piece) // verdict._COPY_DIVISOR


@pytest.mark.parametrize("wrappers", [3, 5, 6])
@pytest.mark.parametrize("fault", ['["x" "y"]', '{"x" "y"}'], ids=["missing-comma", "missing-colon"])
def test_a_wrapped_fault_at_a_quote_does_not_spend_the_budget(wrappers, fault):
    # Each wrapper's decode stops at the same quote early on; an unbounded decoder reads the
    # later verdict, and so must the bounded scan (charging each wrapper its whole extent, about
    # 94 KB, refused from five wrappers on).
    pad = ",".join(['"' + "z" * 60 + '"'] * 1500)
    text = ("Here is my analysis:\n" + '{"w": ' * (wrappers - 1) + '{"notes": ' + fault
            + ', "pad": [' + pad + "]" + "}" * wrappers + '\nFinal: {"verdict": "REFUTED", "reason": "ok"}')
    assert len(text) > 90_000
    result = verdict.parse_verdict(text)
    assert (result.verdict, result.parse_mode) == ("REFUTED", "json")


def test_a_failed_decode_reads_only_its_container(monkeypatch):
    # JSONDecodeError counts the lines before its position, so a failure decoded in place would
    # cost the whole reply before it: quadratic over many cheap failures.
    pieces = []
    decoder = json.JSONDecoder()

    class RecordingDecoder:
        def raw_decode(self, piece, index):
            pieces.append(len(piece))
            return decoder.raw_decode(piece, index)

    monkeypatch.setattr(verdict, "_DECODER", RecordingDecoder())
    result = verdict.parse_hypotheses("[0 x]\n" * 5_000 + '[{"hypothesis":"kept"}]')
    assert [item.hypothesis for item in result] == ["kept"]
    assert len(pieces) == 5_001 and max(pieces[:-1]) == len("[0 x]"), (len(pieces), max(pieces))


def _interval_reply(count, length):
    """Prose that cites ``count`` half-open intervals (``[0, len)``, never closed) in ``length`` chars."""
    lines = []
    for n in range(count):
        line = f"Check {n}: the index must be in [0, len) before buf access. "
        lines.append(line + "y" * max(0, length // count - len(line) - 1) + "\n")
    return "".join(lines)


@pytest.mark.parametrize("count, length", [(10, 60_000), (12, 96_000), (20, 80_000), (30, 100_000),
                                           (30, 60_000)])
def test_interval_notation_in_a_long_reply_parses_like_an_unbounded_decoder(count, length):
    # Each ``[`` is unmatched, so its extent runs to the end of the reply; looking it up must not
    # cost a walk per opener. An unbounded decoder returns the trailing answers.
    body = _interval_reply(count, length)
    assert 60_000 <= len(body) <= 100_000
    hypotheses = verdict.parse_hypotheses(body + '\n[{"hypothesis":"kept"}]')
    assert [item.hypothesis for item in hypotheses] == ["kept"]
    result = verdict.parse_verdict(body + '\n{"verdict":"REFUTED"}')
    assert (result.verdict, result.parse_mode) == ("REFUTED", "json")


@pytest.mark.parametrize("count", [64, 65, 66, 100])
def test_unmatched_brackets_never_refuse_for_depth(monkeypatch, count):
    # Every later unmatched ``[`` nests inside the first, so the first opens ``count`` levels,
    # but a container that never closes can never decode: an unbounded decoder fails on each and
    # returns the trailing answer, and so must the bounded scan, without decoding any of them.
    body = _interval_reply(count, 200 * count)
    with pytest.raises(verdict.VerdictParseError, match="no hypothesis list"):
        verdict.parse_hypotheses(body)
    handed, _passes = _record_work(monkeypatch)
    hypotheses = verdict.parse_hypotheses(body + '\n[{"hypothesis":"kept"}]')
    assert [item.hypothesis for item in hypotheses] == ["kept"]
    assert len(handed) == 1, handed


@pytest.mark.parametrize("prefix", ["", "[0, len) " * 70 + "\n"], ids=["alone", "after-unmatched"])
def test_closed_nesting_past_the_depth_bound_still_refuses(prefix):
    # A real container deeper than DEFAULT_MAX_DEPTH is refused where it stands, even among
    # unmatched openers, and a later answer is not promoted past it.
    with pytest.raises(verdict.VerdictParseError, match="nesting bound"):
        verdict.parse_hypotheses(prefix + _arrays(65) + '\n[{"hypothesis":"kept"}]')
    with pytest.raises(verdict.VerdictParseError, match="nesting bound"):
        verdict.parse_verdict(prefix + '{"x":' + _arrays(64) + '}\n{"verdict":"REFUTED"}')


_EXTENT_ALPHABETS = ('[]{}"\\', '[]{}"\\a ,:', '[[[]]{}"', '["\\]', '{}"\\\\x')


def test_container_extents_match_one_walk_per_opener():
    # The one-pass table must agree with a fresh walk from every opener, quotes and escapes
    # included (a walk that starts inside a string reads it differently).
    rng = random.Random(7)
    checked = 0
    for trial in range(6_000):
        alphabet = _EXTENT_ALPHABETS[trial % len(_EXTENT_ALPHABETS)]
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        expected = {}
        for index, char in enumerate(text):
            if char in "[{":
                deepest, end, closed = container_walk(text, index, max_depth=10**9)
                assert (deepest, end) == container_extent(text, index, max_depth=10**9)
                expected[index] = (deepest, end if closed else -1)
        table = container_extents(text)
        assert dict(table.items()) == expected, text
        assert all(table.lookup(index) == extent for index, extent in expected.items()), text
        checked += len(expected)
    assert checked > 10_000


def test_container_extents_lookup_refuses_offsets_without_an_opener():
    table = container_extents('a["]')
    assert table.lookup(1) == (1, -1)  # the quote's string runs to the end: never closes
    for offset in (-1, 0, 2, 3, 4):
        with pytest.raises(KeyError):
            table.lookup(offset)
