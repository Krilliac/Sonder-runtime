# ruff: noqa: UP031  (printf-style formats keep %.200r precision, which f-strings cannot express)
"""Seeded mutation fuzzing of every parser that reads text a model wrote.

Contract: model output is hostile. Each parser either returns a value that
satisfies its documented invariants or raises only its own typed error, and it
does so inside the time budget (p99 under 50 ms per input, hard cap 2 s). No
other exception (RecursionError, OverflowError, TypeError, ...) may escape.

Functions under test:
  security_eval.verdict      parse_verdict, parse_hypotheses      (VerdictParseError)
  security_eval.records      parse_record, parse_line_range,
                             check_relative_path                  (DatasetRecordError)
  consult                    _judge_verdict, _substance           (never raise)
  agents.decision_parsing    extract_agent_json                   (ValueError)
  agents.tool_args           normalize_tool_args,
                             normalize_decision_aliases           (never raise, never mutate)
  json_reply                 check_reply                          (invalid JSON is a status)
  computer_use.rules         parse_model_step, parse_chord        (ActionRefused), normalize_app
  meshgen.critique           parse_critique                       (returns a failure dict)

The seed is SONDER_FUZZ_SEED (default below) and is part of every failure
message, so a CI failure replays with the same environment variable. Hostile
"blob" inputs are 30 000 characters, which a linear scan handles in a few
milliseconds; they get a 0.1 s budget because the 2 s hard cap would let a
quadratic scan of that size (about 0.25 s) through unnoticed.
"""
from __future__ import annotations

import copy
import json
import ntpath
import os
import posixpath
import random
import re
import time
from pathlib import PureWindowsPath

import pytest

import consult
from sonder_runtime.domain.agents.decision_parsing import extract_agent_json
from sonder_runtime.domain.agents.tool_args import (
    normalize_decision_aliases,
    normalize_tool_args,
)
from sonder_runtime.domain.computer_use import rules
from sonder_runtime.domain.json_reply import UNSUPPORTED_SCHEMA, ReplyCheck, check_reply
from sonder_runtime.domain.meshgen.critique import MAX_ISSUES, parse_critique
from sonder_runtime.domain.security_eval.errors import DatasetRecordError
from sonder_runtime.domain.security_eval.records import (
    LABELS,
    MAX_FIELD_CHARS,
    MAX_RANGE_SPAN,
    STAGES,
    HypothesisRecord,
    LineRange,
    check_relative_path,
    parse_line_range,
    parse_record,
)
from sonder_runtime.domain.security_eval.scoring import match_findings
from sonder_runtime.domain.security_eval.verdict import (
    VERDICTS,
    ParsedHypothesis,
    ParsedVerdict,
    VerdictParseError,
    parse_hypotheses,
    parse_verdict,
)

pytestmark = pytest.mark.unit

SEED = int(os.environ.get("SONDER_FUZZ_SEED", "20261116"))
ITER = 1500
BLOB = 30_000
BLOB_BUDGET = 0.1


# --------------------------------------------------------------------------- harness

def _rng(salt: int) -> random.Random:
    return random.Random(SEED * 1000 + salt)


def _where(failures: list[str]) -> str:
    return "SONDER_FUZZ_SEED=%d; %d failure(s), first:\n  %s" % (SEED, len(failures), "\n  ".join(failures[:5]))


class _Run:
    """Calls a function, times it and records every unexpected exception instead of stopping."""

    def __init__(self) -> None:
        self.timings: list[float] = []
        self.failures: list[str] = []

    def call(self, fn, args: tuple, allowed: tuple, budget: float = 2.0):
        """Return ``(returned, value_or_error)``; ``returned`` is None when the call failed the contract."""
        start = time.perf_counter()
        try:
            value = fn(*args)
            outcome = (True, value)
        except allowed as exc:
            outcome = (False, exc)
        except Exception as exc:  # noqa: BLE001 - the point is to catch any other type
            self.failures.append("%s raised %s: %.80s on %.200r" % (fn.__name__, type(exc).__name__, exc, args))
            return None, None
        elapsed = time.perf_counter() - start
        self.timings.append(elapsed)
        if elapsed >= budget:
            self.failures.append("%s took %.0f ms (budget %.0f ms) on input of %d chars %.80r" % (
                fn.__name__, elapsed * 1000, budget * 1000, len(repr(args)), args))
        return outcome

    def check(self, label: str, *, latency: bool = True) -> None:
        assert not self.failures, "%s: %s" % (label, _where(self.failures))
        if latency and self.timings:
            ordered = sorted(self.timings)
            p99 = ordered[max(0, int(len(ordered) * 0.99) - 1)]
            assert p99 < 0.05, "%s: p99 %.1f ms (SONDER_FUZZ_SEED=%d)" % (label, p99 * 1000, SEED)
            assert ordered[-1] < 2.0, "%s: max %.1f ms (SONDER_FUZZ_SEED=%d)" % (label, ordered[-1] * 1000, SEED)


def _mutate(rng: random.Random, text: str, fragments: tuple[str, ...], donors: tuple[str, ...]) -> str:
    chars = list(text)
    for _ in range(rng.randint(1, 4)):
        op = rng.randrange(7)
        at = rng.randrange(len(chars) + 1)
        if op == 0 and chars:
            chars[min(at, len(chars) - 1)] = chr(rng.choice((0, 9, 10, 13, 32, 34, 92, 123, 125, 91, 93, 0x7F, 0x2028,
                                                             0xFF21, rng.randrange(0x20, 0x7F))))
        elif op == 1:
            chars[at:at] = rng.choice(fragments)
        elif op == 2:
            del chars[at:at + rng.randint(1, 12)]
        elif op == 3:
            donor = rng.choice(donors)
            start = rng.randrange(len(donor) + 1)
            chars[at:at] = donor[start:start + rng.randint(1, 40)]
        elif op == 4 and chars:
            span = chars[at:at + rng.randint(1, 8)]
            chars[at:at] = span * rng.randint(2, 40)
        elif op == 5:
            del chars[at:]
        else:
            chars[at:at] = rng.choice(fragments)
    return "".join(chars)


def _fuzz_inputs(salt: int, seeds: tuple[str, ...], fragments: tuple[str, ...], count: int = ITER) -> list[str]:
    rng = _rng(salt)
    return [_mutate(rng, rng.choice(seeds), fragments, seeds) for _ in range(count)]


def _case(rng: random.Random, text: str) -> str:
    return "".join(ch.upper() if rng.random() < 0.5 else ch.lower() for ch in text)


_WORDS = ("the", "answer", "is", "below", "analysis", "of", "this", "code", "path", "because", "eyes", "NOTE",
          "nobody", "know", "yesterday", "snow", "yes", "no", "not", "result", "follows", "so", "far", "ok.", "-", "*")


def _prose(rng: random.Random, words: tuple[str, ...] = _WORDS) -> str:
    """Benign filler with no braces, brackets, quotes, backticks or angle brackets."""
    return " ".join(rng.choice(words) for _ in range(rng.randint(0, 12)))


def _blobs(n: int = BLOB) -> dict[str, str]:
    return {
        "open-braces": "{" * n,
        "open-brackets": "[" * n,
        "nested-objects": '{"a":' * (n // 5),
        "nested-arrays": '{"a":' + "[" * n,
        "brace-then-text": "{" * n + "x",
        "unbalanced-in-fence": "```json\n" + "{" * n,
        "unterminated-think": "<think>" + "{" * n,
        "think-spam": "<think>" * (n // 7),
        "quotes": '"' * n,
        "backslashes": '{"a":"' + "\\" * n,
        "whitespace-run": "ok" + " " * n + "x",
        "footer-rows": "a\n=== ENSEMBLE (2 models answered) ===" + "\n  row" * (n // 6) + "\nX",
        "newline-space-runs": "\n  " * (n // 3) + "x",
        "fence-spam": "```\n" * (n // 4),
        "many-labels": "VERDICT: REFUTED\n" * (n // 17),
    }


# --------------------------------------------------------------------------- security_eval.verdict

_VERDICT_SEEDS = (
    '{"verdict": "CONFIRMED", "confidence": 0.8, "reason": "no bound check"}',
    'Here you go:\n```json\n{"verdict": "refuted", "reason": "guarded"}\n```\nDone.',
    '<think>first {"verdict": "REFUTED"}</think>\n{"verdict": "UNCERTAIN", "confidence": 1}',
    "VERDICT: CONFIRMED\nbecause the copy is unbounded",
    "**Verdict:** refuted",
    '[{"file": "src/a.c", "lines": "10-20", "category": "oob", "hypothesis": "len unchecked", "severity": "high"}]',
    '{"hypotheses": [{"file": "a\\\\b.c", "lines": [3, 4], "hypothesis": "x", "category": "uaf"}]}',
    '```json\n[{"hypothesis": "h", "file": "../../etc/passwd", "lines": 7}]\n```',
)
_VERDICT_FRAGMENTS = (
    "{", "}", "[", "]", '"', "\\", "<think>", "</think>", "```", "```json\n", "VERDICT: CONFIRMED", "\nverdict = refuted",
    "\x00", "\u2028", '{"verdict":"CONFIRMED"}', '"lines": "1-99999"', '"file": "/etc/passwd"', '"file": "C:\\\\x"',
    "null", "1e999", "NaN", "\r\n", ',"confidence": 7', '"verdict": "uncertain"', "\u0131", "\u212a",
    '{"hypotheses": []}', "[]", '"lines": [0, 3]', '"lines": "5-2"',
)


def _check_verdict(result: ParsedVerdict) -> str | None:
    if not isinstance(result, ParsedVerdict):
        return "not a ParsedVerdict"
    if result.verdict not in VERDICTS:
        return "verdict %r not in VERDICTS" % result.verdict
    if result.confidence is not None and not (isinstance(result.confidence, float) and 0.0 <= result.confidence <= 1.0):
        return "confidence %r out of range" % (result.confidence,)
    if not isinstance(result.reason, str) or len(result.reason) > 500:
        return "reason not a str of <= 500 chars"
    if result.parse_mode not in ("json", "fenced_json", "label_line"):
        return "parse_mode %r" % result.parse_mode
    if result.parse_mode == "label_line" and (result.confidence is not None or result.reason):
        return "label_line verdict must carry no confidence or reason"
    return None


def test_parse_verdict_fuzz_contract():
    run = _Run()
    for text in _fuzz_inputs(1, _VERDICT_SEEDS, _VERDICT_FRAGMENTS):
        returned, value = run.call(parse_verdict, (text,), (VerdictParseError,))
        if returned:
            problem = _check_verdict(value)
            if problem:
                run.failures.append("%s on %.200r" % (problem, text))
    run.check("parse_verdict")


def test_parse_verdict_non_text_input_raises_typed_error():
    run = _Run()
    for value in (None, 0, 1.5, b'{"verdict":"CONFIRMED"}', [], {}, ("x",), object()):
        returned, _ = run.call(parse_verdict, (value,), (VerdictParseError,))
        assert returned is False, "non-text %r must raise VerdictParseError" % (value,)
    run.check("parse_verdict non-text")


def test_parse_verdict_recovers_planted_json_in_random_context():
    rng = _rng(2)
    for index in range(ITER):
        verdict = rng.choice(VERDICTS)
        planted = '{"verdict": "%s", "confidence": %s, "reason": "r"}' % (_case(rng, verdict), rng.choice(("0.5", "1", "0")))
        decoys = rng.choice(("", "VERDICT: REFUTED\n", '{"verdict": "MAYBE"}\n', '{"other": 1}\n', "{not json}\n",
                             '<think>{"verdict": "REFUTED"}</think>\n', '<think>{"verdict": "REFUTED"} unterminated</think>'))
        wrap = rng.choice(("%s", "```json\n%s\n```", "```\n%s\n```"))
        text = _prose(rng) + "\n" + decoys + (wrap % planted) + "\n" + _prose(rng)
        parsed = parse_verdict(text)
        assert parsed.verdict == verdict, "seed %d iter %d: %r -> %r" % (SEED, index, text, parsed)
        assert parsed.parse_mode == ("json" if wrap == "%s" else "fenced_json"), "seed %d iter %d: %r" % (SEED, index, text)


def test_parse_verdict_label_line_rules():
    rng = _rng(3)
    for index in range(ITER):
        verdict = rng.choice(VERDICTS)
        prefix = "".join(rng.choice(" \t>*_#-") for _ in range(rng.randint(0, 4)))
        line = "%sVERDICT%s%s%s%s" % (prefix, rng.choice(("", " ", "**", "_")), rng.choice((":", "=", " :", ": ")),
                                       rng.choice(("", " ", "**")), _case(rng, verdict))
        text = _prose(rng) + "\n" + line + "\n" + _prose(rng)
        parsed = parse_verdict(text)
        assert (parsed.verdict, parsed.parse_mode) == (verdict, "label_line"), "seed %d iter %d: %r" % (SEED, index, text)
        other = rng.choice([v for v in VERDICTS if v != verdict])
        with pytest.raises(VerdictParseError):
            parse_verdict(text + "\nVERDICT: " + other)
        assert parse_verdict(text + "\nVERDICT: " + verdict).verdict == verdict
        # a bare mention in prose is not a verdict line
        with pytest.raises(VerdictParseError):
            parse_verdict(_prose(rng) + " the verdict is " + verdict + " " + _prose(rng))


def test_parse_verdict_think_block_never_supplies_a_verdict():
    rng = _rng(4)
    for index in range(ITER):
        hidden = rng.choice(VERDICTS)
        think = "<think>%s {\"verdict\": \"%s\"}\nVERDICT: %s</think>" % (_prose(rng), hidden, hidden)
        text = _case(rng, think) if rng.random() < 0.3 else think
        with pytest.raises(VerdictParseError):
            parse_verdict(text + _prose(rng))
        with pytest.raises(VerdictParseError):  # an unterminated think discards everything after it
            parse_verdict(think[:-len("</think>")] + '{"verdict": "CONFIRMED"}')
        real = rng.choice(VERDICTS)
        assert parse_verdict(text + '\n{"verdict": "%s"}' % real).verdict == real, "seed %d iter %d" % (SEED, index)


def test_parse_hypotheses_fuzz_contract_and_bounds():
    run = _Run()
    rng = _rng(5)
    for text in _fuzz_inputs(5, _VERDICT_SEEDS + ('[' + ",".join(['{"hypothesis": "h%d", "lines": %d}' % (i, i + 1)
                                                                  for i in range(80)]) + ']',), _VERDICT_FRAGMENTS):
        max_items = rng.choice((0, 1, 2, 3, 50, 100))
        returned, value = run.call(parse_hypotheses, (text, max_items), (VerdictParseError,))
        if not returned:
            continue
        if not isinstance(value, tuple) or len(value) > max_items:
            run.failures.append("result size %s exceeds max_items %d on %.200r" % (len(value), max_items, text))
            continue
        for item in value:
            problem = _hypothesis_problem(item)
            if problem:
                run.failures.append("%s: %r on %.200r" % (problem, item, text))
    run.check("parse_hypotheses")


def _hypothesis_problem(item: object) -> str | None:
    if not isinstance(item, ParsedHypothesis):
        return "not a ParsedHypothesis"
    if not item.hypothesis or item.hypothesis != item.hypothesis.strip() or len(item.hypothesis) > 4000:
        return "hypothesis text invalid"
    if len(item.category) > 64 or len(item.severity) > 32 or len(item.file) > 1024:
        return "field over its limit"
    if "\\" in item.file:
        return "backslash left in file"
    if item.lines is not None and not (isinstance(item.lines, LineRange) and 1 <= item.lines.start <= item.lines.end
                                       and item.lines.span <= MAX_RANGE_SPAN):
        return "line range invalid"
    return None


def test_parse_hypotheses_max_items_boundaries():
    body = "[" + ",".join('{"hypothesis": "h%d"}' % i for i in range(10)) + "]"
    for limit in (0, 1, 9, 10, 11, 50):
        assert len(parse_hypotheses(body, limit)) == min(limit, 10)
    assert parse_hypotheses("[]") == ()
    assert parse_hypotheses('{"hypotheses": []}') == ()
    with pytest.raises(VerdictParseError):
        parse_hypotheses('[{"no": "hypothesis"}]')


def test_parse_hypotheses_recovers_planted_array_in_random_context():
    rng = _rng(6)
    for index in range(ITER):
        count = rng.randint(1, 6)
        items = [{"hypothesis": "h%d %s" % (i, _prose(rng) or "x"), "file": "src/f%d.c" % i, "lines": "%d-%d" % (i + 1, i + 5),
                  "category": "c", "severity": "low"} for i in range(count)]
        planted = json.dumps(items) if rng.random() < 0.5 else json.dumps({"hypotheses": items})
        wrap = rng.choice(("%s", "```json\n%s\n```"))
        text = _prose(rng) + "\n" + wrap % planted + "\n" + _prose(rng)
        found = parse_hypotheses(text, 50)
        assert [h.hypothesis for h in found] == [i["hypothesis"].strip() for i in items], "seed %d iter %d" % (SEED, index)
        assert [h.lines.as_text() for h in found] == [i["lines"] for i in items]


def test_hostile_hypothesis_paths_never_match_a_finding():
    """The model's ``file`` is only string-compared, never opened, so it is not validated: a hostile
    value must simply fail to equal the record's file. Blanking it would make it mean "the chunk's own
    file" (runner.py passes ``unit.file`` as ``default_file``) and let it match, inflating eval scores."""
    bs = chr(92)
    record = HypothesisRecord(hid="h01", repo_root="repo", file="src/Reader.cs", lines=LineRange(10, 12), category="oob",
                              hypothesis="h", label="TP", severity="high", stage="s", reason="")
    hostile = ("../../etc/passwd", "../other/Thing.cs", "C:/elsewhere/x.cs", bs * 2 + "server" + bs + "share" + bs + "x",
               "src/Reader.cs" + chr(0), "/etc/passwd", "a" + bs + ".." + bs + ".." + bs + "b")
    for name in hostile:
        text = json.dumps([{"hypothesis": "h", "file": name, "lines": "10-12"}])
        findings = parse_hypotheses(text)
        assert len(findings) == 1 and findings[0].file != "", "file %r was blanked" % (name,)
        assert match_findings(findings, (record,), 0, "src/Reader.cs") == (), "hostile file %r matched" % (name,)
    own = parse_hypotheses(json.dumps([{"hypothesis": "h", "file": "src/Reader.cs", "lines": "10-12"}]))
    assert match_findings(own, (record,), 0, "src/Reader.cs") == (record,)
    omitted = parse_hypotheses(json.dumps([{"hypothesis": "h", "lines": "10-12"}]))
    assert match_findings(omitted, (record,), 0, "src/Reader.cs") == (record,)


# --------------------------------------------------------------------------- security_eval.records

def _is_plain_relative(path: str) -> bool:
    """Independent oracle: is ``path`` safe to join under a Windows or POSIX repo root?"""
    if not path or "\x00" in path or ":" in path or "\\" in path or path.startswith("/"):
        return False
    if ntpath.splitdrive(path)[0] or ntpath.isabs(path) or posixpath.isabs(path):
        return False
    pure = PureWindowsPath(path)
    if pure.drive or pure.anchor or pure.root:
        return False
    segments = path.split("/")
    if any(seg in ("", ".", "..") or seg != seg.rstrip(" .") for seg in segments):
        return False
    root = "C:\\repo"
    joined = ntpath.normpath(ntpath.join(root, path))
    return joined.startswith(root + "\\") and ".." not in joined.split("\\")


_PATH_ALPHABET = ("a", "b", "src", ".", "..", "...", "/", "\\", "//", "\\\\", ":", "C:", "c:\\", " ", "\t", "\x00", "~1", "x.c",
                  "con", "\n", "\u00a0", "\u2024", "/etc", "$", "*", "?", "%2e", "%2f")


def test_check_relative_path_fuzz_oracle():
    rng = _rng(8)
    run = _Run()
    accepted = 0
    for _ in range(ITER * 3):
        path = "".join(rng.choice(_PATH_ALPHABET) for _ in range(rng.randint(0, 9)))
        returned, value = run.call(check_relative_path, (path,), (DatasetRecordError,))
        if not returned:
            continue
        accepted += 1
        if not _is_plain_relative(value):
            run.failures.append("accepted %r -> %r which the oracle rejects" % (path, value))
        elif check_relative_path(value) != value:
            run.failures.append("not idempotent: %r -> %r -> %r" % (path, value, check_relative_path(value)))
        elif not _is_plain_relative(path.strip().replace("\\", "/").replace("//", "/").strip("/")) and ".." in path.split("/"):
            run.failures.append("accepted %r containing a .. segment" % path)
    run.check("check_relative_path fuzz")
    assert accepted > 100, "oracle fuzz produced only %d accepted paths; the alphabet is too hostile to test anything" % accepted


def test_check_relative_path_never_accepts_planted_escape():
    rng = _rng(9)
    seg = ("a", "src", "Lib", "x.c", "dir.d", "A B")
    sep = ("/", "\\", "//", "\\\\", "/\\", "\\/")
    escapes = (
        lambda: "..", lambda: "../..", lambda: "a/../..", lambda: "a/../b", lambda: "./..", lambda: "..\\x",
        lambda: "a\\..\\..\\b", lambda: "...//..", lambda: "/etc/passwd", lambda: "\\Windows", lambda: "\\\\server\\share\\f",
        lambda: "//server/share", lambda: "C:\\x", lambda: "c:x", lambda: "Z:/x", lambda: "a:b", lambda: "f.txt:stream",
        lambda: "f.txt::$DATA", lambda: "a\x00b", lambda: "x\x00", lambda: "a/b. ", lambda: "a./b", lambda: "a/b ./c",
        lambda: "dir/", lambda: "", lambda: "   ", lambda: ".", lambda: "./", lambda: "//",
    )
    for index in range(ITER * 2):
        core = rng.choice(escapes)()
        parts = [rng.choice(seg) for _ in range(rng.randint(0, 2))]
        before = "".join(p + rng.choice(sep) for p in parts) if rng.random() < 0.5 and core and core[0] not in "/\\" else ""
        after = (rng.choice(sep) + rng.choice(seg)) if rng.random() < 0.4 and core.strip() and "\x00" not in core else ""
        pad = rng.choice(("", " ", "\t", "\n", "\u00a0"))
        path = pad + before + core + after + pad
        if rng.random() < 0.3:
            path = path.upper()
        if not _contains_escape(path):
            continue  # padding or decoration made it a legitimate path (for example dir/ + x)
        try:
            accepted = check_relative_path(path)
        except DatasetRecordError:
            continue
        raise AssertionError("seed %d iter %d: %r was accepted as %r" % (SEED, index, path, accepted))


def _contains_escape(path: str) -> bool:
    text = path.strip().replace("\\", "/")
    return (text.startswith("/") or ":" in text or "\x00" in text or ".." in text.split("/") or text.endswith("/")
            or not [p for p in text.split("/") if p not in ("", ".")]
            or any(p != p.rstrip(" .") for p in text.split("/") if p not in ("", ".")))


@pytest.mark.parametrize("path", ["../x", "a/../..", "a\\..\\..\\b", "..", "/abs", "\\abs", "\\\\server\\share", "//server/share",
                                  "C:\\x", "c:x", "a:b", "f:ads", "a\x00b", "a/b.", " ", "", "dir/", "./", ".", "a/..", "a/b /c",
                                  "..\\", "a/./../../b"])
def test_check_relative_path_known_escapes(path):
    with pytest.raises(DatasetRecordError):
        check_relative_path(path)


@pytest.mark.parametrize("path, expected", [("a/b.c", "a/b.c"), (" a\\b.c ", "a/b.c"), ("./a//b", "a/b"), ("a/.b", "a/.b"),
                                            ("a/..b", "a/..b"), ("a/b..c", "a/b..c")])
def test_check_relative_path_normalises_good_paths(path, expected):
    assert check_relative_path(path) == expected


def test_check_relative_path_non_string_and_oversize():
    for value in (None, 1, b"a", ["a"], {"a": 1}, object()):
        with pytest.raises(DatasetRecordError):
            check_relative_path(value)
    assert check_relative_path("a" * MAX_FIELD_CHARS["file"]) == "a" * MAX_FIELD_CHARS["file"]
    with pytest.raises(DatasetRecordError):
        check_relative_path("a" * (MAX_FIELD_CHARS["file"] + 1))


def test_parse_line_range_oracle():
    rng = _rng(10)
    run = _Run()
    for _ in range(ITER * 3):
        start = rng.choice((-5, -1, 0, 1, 2, 17, 399, 400, 401, 10**9 - 1, 10**9, 10**12))
        end = start + rng.choice((-2, -1, 0, 0, 1, 5, 398, 399, 400, 10**6))
        span = rng.choice((MAX_RANGE_SPAN, 1, 10, 400, 1000))
        pad = (" " * rng.randint(0, 2), " " * rng.randint(0, 2))
        form = rng.randrange(4)
        value = (("%s%d%s-%s%d%s" % (pad[0], start, pad[1], pad[1], end, pad[0])) if form == 0 else
                 ("%d" % start) if form == 1 else [start, end] if form == 2 else (start, end))
        if form == 1:
            end = start
        too_many_digits = form in (0, 1) and (len(str(abs(start))) > 9 or len(str(abs(end))) > 9 or start < 0 or end < 0)
        expect_ok = 1 <= start <= end and end - start + 1 <= span and not too_many_digits
        returned, got = run.call(parse_line_range, (value,), (DatasetRecordError,)) if span == MAX_RANGE_SPAN else \
            _call_span(run, value, span)
        if bool(returned) != expect_ok:
            run.failures.append("%r max_span=%d: accepted=%s expected=%s" % (value, span, bool(returned), expect_ok))
        elif returned and (got.start, got.end) != (start, end):
            run.failures.append("%r parsed as %s-%s" % (value, got.start, got.end))
    run.check("parse_line_range oracle")


def _call_span(run: _Run, value: object, span: int):
    return run.call(lambda v: parse_line_range(v, max_span=span), (value,), (DatasetRecordError,))


def test_parse_line_range_fuzz_invariants_and_error_contract():
    rng = _rng(11)
    run = _Run()
    pool = (True, False, None, 0, 1, -1, 2**63, 2**200, 1.0, 1.5, float("nan"), float("inf"), "", " ", "1-", "-1", "1--2", "1-2-3",
            "0", "٣-٤", "1_0", "1e3", "０１", "1 -\n2", "1\x00", "9" * 10, "9" * 5000, [], [1], [1, 2, 3], [1.0, 2], [True, 2], [None, 2],
            (1, 2), {"a": 1}, b"1-2", object(), "5-3", "400", "1-400", "1-401", "  7  ", "7-7", "007-009")
    for _ in range(ITER * 2):
        value = rng.choice(pool)
        returned, got = run.call(parse_line_range, (value,), (DatasetRecordError,))
        if returned and not (1 <= got.start <= got.end and got.span <= MAX_RANGE_SPAN and got.span >= 1):
            run.failures.append("%r accepted as invalid range %r" % (value, got))
    run.check("parse_line_range pool")


_RECORD_BASE = {"hid": "h1", "repo_root": "/srv/repo", "file": "src/a.c", "lines": "10-20", "category": "oob",
                "hypothesis": "length is unchecked", "label": "confirmed", "stage": "R1", "severity": "high", "reason": "r"}


def _random_value(rng: random.Random, depth: int = 0):
    kind = rng.randrange(12 if depth < 2 else 8)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice((True, False))
    if kind == 2:
        return rng.choice((0, 1, -1, 7, 2**31, 2**70))
    if kind == 3:
        return rng.choice((0.0, 1.5, -2.25, 1e300))
    if kind in (4, 5):
        return rng.choice(("", " ", "x", "a" * rng.choice((10, 70, 130, 1100, 4100)), "..\\..\\x", "/etc/passwd", "C:\\x", "a\x00b",
                           "CONFIRMED", "refuted ", "r1", "NEW", "1-3", "3-1", "\u2028", "\n"))
    if kind == 6:
        return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    if kind == 7:
        return {"k": _random_value(rng, depth + 1)}
    return rng.choice(("src/a.c", "a/../b", "x:y", "ok"))


def _record_problem(rec: object) -> str | None:
    if not isinstance(rec, HypothesisRecord):
        return "not a HypothesisRecord"
    if not _is_plain_relative(rec.file):
        return "file %r is not a plain relative path" % rec.file
    if rec.label not in LABELS or rec.stage not in STAGES:
        return "label/stage out of the closed sets"
    if not (1 <= rec.lines.start <= rec.lines.end and rec.lines.span <= MAX_RANGE_SPAN):
        return "line range invalid"
    for name in MAX_FIELD_CHARS:
        value = getattr(rec, name)
        if len(value) > MAX_FIELD_CHARS[name] or "\x00" in value or (name != "reason" and value != value.strip()):
            return "field %s violates its limits" % name
    for name in ("hid", "repo_root", "category", "hypothesis"):
        if not getattr(rec, name):
            return "required field %s empty" % name
    return None


def test_parse_record_fuzz_contract():
    rng = _rng(12)
    run = _Run()
    accepted = 0
    for line_no in range(1, ITER * 2 + 1):
        obj = dict(_RECORD_BASE)
        for _ in range(rng.randint(0, 3)):
            key = rng.choice(list(_RECORD_BASE) + ["extra"])
            if rng.random() < 0.25:
                obj.pop(key, None)
            else:
                obj[key] = _random_value(rng)
        returned, rec = run.call(parse_record, (obj, line_no), (DatasetRecordError,))
        if returned:
            accepted += 1
            problem = _record_problem(rec)
            if problem:
                run.failures.append("%s for %.300r" % (problem, obj))
        elif rec is not None and getattr(rec, "line_no", line_no) != line_no:
            run.failures.append("error lost its line number for %.200r" % (obj,))
    for junk in (None, 1, "x", [], [obj for obj in ()], b"{}", 1.5):
        returned, _ = run.call(parse_record, (junk, 3), (DatasetRecordError,))
        assert returned is False, "non-object %r must be refused" % (junk,)
    run.check("parse_record")
    assert accepted > 50, "only %d records accepted; the mutations are too destructive to test anything" % accepted
    assert parse_record(dict(_RECORD_BASE), 1).file == "src/a.c"


# --------------------------------------------------------------------------- consult

def _yes_no_oracle(text: str) -> str | None:
    """Independent statement of the rule: one line, first word YES/NO, no other YES/NO word."""
    value = text.strip()
    if "\n" in value or "\r" in value:
        return None
    words = re.findall(r"\w+", value)
    hits = [w.upper() for w in words if w.upper() in ("YES", "NO")]
    first = re.match(r"\w+", value)
    if first is None or first.group(0).upper() not in ("YES", "NO") or len(hits) != 1:
        return None
    return hits[0]


_JUDGE_TOKENS = ("YES", "yes", "No", "NO", "no", "Yes", "eyes", "NOTE", "nobody", "know", "yesterday", "snow", "not", "yes-no",
                 "NO_", "_yes", "yes_", "YES.", "No,", "(yes)", "\"yes\"", " ", "  ", ".", ",", ":", "-", "x", "answer", "the", "1")


def test_judge_verdict_matches_oracle_on_ascii_text():
    rng = _rng(13)
    run = _Run()
    seen = {"YES": 0, "NO": 0, None: 0}
    for _ in range(ITER * 4):
        text = rng.choice(("", " ", "\t")) + "".join(rng.choice(_JUDGE_TOKENS) + rng.choice((" ", " ", "", "\n", "\r\n"))
                                                       for _ in range(rng.randint(0, 6)))
        returned, got = run.call(consult._judge_verdict, (text,), ())
        if returned is None:
            continue
        if got not in ("YES", "NO", None):
            run.failures.append("returned %r for %.100r" % (got, text))
            continue
        seen[got] += 1
        if got != _yes_no_oracle(text):
            run.failures.append("%.100r -> %r, oracle says %r" % (text, got, _yes_no_oracle(text)))
    run.check("_judge_verdict oracle")
    assert min(seen.values()) > 20, "the generator did not exercise every outcome: %r" % (seen,)


def test_judge_verdict_conflicting_markers_are_none_not_a_pick():
    rng = _rng(14)
    for _ in range(ITER):
        first, second = rng.sample(("YES", "NO"), 2)
        glue = rng.choice((", but ", " / ", " and ", ". ", " - ", ": "))
        text = _case(rng, first) + glue + _prose(rng) + " " + _case(rng, second) + " " + _prose(rng)
        assert consult._judge_verdict(text) is None, "%r picked a side" % text
        assert consult._judge_verdict(_case(rng, first) + glue + _case(rng, first)) is None, "repeated marker is also ambiguous"


def test_judge_verdict_decoy_words_do_not_flip_the_verdict():
    rng = _rng(15)
    decoys = ("eyes", "NOTE", "nobody", "know", "yesterday", "snow", "not", "notes", "yeses", "nope", "casino", "bayes", "NOW")
    for _ in range(ITER):
        verdict = rng.choice(("YES", "NO"))
        tail = " ".join(rng.choice(decoys) for _ in range(rng.randint(0, 6)))
        text = rng.choice(("", " ", "\n")) + _case(rng, verdict) + rng.choice((".", ",", ":", "", " -", "!")) + " " + tail
        assert consult._judge_verdict(text) == verdict, "%r should be %s" % (text, verdict)
    for text in ("eyes YES", "NOTE: YES", "nobody said NO", "The answer is YES", "yesterday", "NOTE", "nobody", "no"[:1], "Y E S"):
        assert consult._judge_verdict(text) is None, "%r must not be a verdict" % text


def test_judge_verdict_fuzz_returns_only_yes_no_or_none():
    run = _Run()
    seeds = ("YES, they agree.", "NO - the answers differ", "yes", "Yes.\nNo", "NOTE: eyes", "", "YES" * 5)
    for text in _fuzz_inputs(16, seeds, ("YES", "NO", "\n", "\r", "\u2028", "\x85", "\x00", "\u0131", "\u017f", "\u212a", "=== ENSEMBLE")):
        returned, got = run.call(consult._judge_verdict, (text,), ())
        if returned is not None and got not in ("YES", "NO", None):
            run.failures.append("returned %r for %.100r" % (got, text))
    for value in (None, 0, False, [], b"YES", object()):
        returned, got = run.call(consult._judge_verdict, (value,), ())
        if returned is not None and got not in ("YES", "NO", None):
            run.failures.append("returned %r for %r" % (got, value))
    run.check("_judge_verdict fuzz")


_FOOTER = "\n=== ENSEMBLE (%d model%s answered) ===%s"


def _footer(rng: random.Random) -> str:
    count = rng.randint(1, 4)
    rows = "".join("\n  %s: %dms%s" % (rng.choice(("code", "reasoning", "cloud-general")), rng.randint(0, 99999),
                                       rng.choice(("", " ok", " \t", "  "))) for _ in range(rng.randint(1, 4)))
    return _FOOTER % (count, "" if count == 1 else "s", rows)


def _body(rng: random.Random) -> str:
    return "".join(rng.choice(("The answer is 42.", "\n", "\n\n", "  indented", "=== not a footer ===", "ENSEMBLE", "(2 models answered)",
                               "```", "\n  row", " ", "ms", "é", "\r\n")) for _ in range(rng.randint(0, 8)))


def test_substance_ignores_only_the_trailing_footer():
    rng = _rng(17)
    for index in range(ITER * 2):
        body = _body(rng)
        footer = _footer(rng)
        assert consult._substance(body + footer) == consult._substance(body), "seed %d iter %d: %r" % (SEED, index, body + footer)
        assert consult._substance(body) == body.strip(), "a footer-free text is only stripped: %r" % body
        # A footer followed by more answer text, or with a CRLF join, is not the documented trailing footer.
        tail = rng.choice(("\nmore answer", "\n=== ENSEMBLE (x models answered) ==="))
        assert "=== ENSEMBLE" in consult._substance(body + footer + tail), "mid-text footer was stripped: %r" % (body + footer + tail)
    assert consult._substance(None) == "" and consult._substance("") == ""
    assert consult._substance("A\r\n=== ENSEMBLE (1 model answered) ===\r\n  c: 1ms") != "A", "CRLF footers are deliberately not matched"


def test_substance_is_idempotent_and_total():
    rng = _rng(18)
    run = _Run()
    fragments = ("\n=== ENSEMBLE (2 models answered) ===\n  code: 3ms", "\n=== ENSEMBLE (", " models answered) ===", "\n  ", "\n", " ",
                 "\x00", "\u2028", "é")
    seeds = tuple(_body(rng) + _footer(rng) for _ in range(6)) + ("plain text", "")
    for text in _fuzz_inputs(18, seeds, fragments):
        if text.count("=== ENSEMBLE (") > 1:
            continue  # stacked footers are covered by the stacked-footer regression below
        returned, once = run.call(consult._substance, (text,), ())
        if returned is None:
            continue
        twice = consult._substance(once)
        if once != twice or not isinstance(once, str) or once != once.strip():
            run.failures.append("not idempotent or not stripped: %.120r -> %.120r -> %.120r" % (text, once, twice))
    for value in (None, 0, False, [], b"x", object()):
        run.call(consult._substance, (value,), ())
    run.check("_substance")


def test_substance_footer_with_trailing_whitespace_regression():
    """A footer followed by trailing whitespace is stripped, the same as when the text is pre-stripped."""
    text = "answer\n=== ENSEMBLE (1 model answered) ===\n  code: 1ms\n"
    assert consult._substance(text) == consult._substance(text.strip()) == "answer"


def test_substance_stacked_footers_are_stripped_to_a_fixpoint():
    """A footer-shaped tail before the real footer is peeled too, so a second pass changes nothing."""
    stacked = "answer" + "".join("\n=== ENSEMBLE (1 model answered) ===\n  code: %dms" % n for n in range(12))
    once = consult._substance(stacked)
    assert once == "answer"
    assert consult._substance(once) == once, "second pass changed %r to %r" % (once, consult._substance(once))


def test_substance_strips_a_footer_shaped_block_the_answer_itself_ended_with():
    """By design: the answer's own trailing footer-shaped block is indistinguishable from a real footer
    and is stripped; _substance only feeds agreement and judge parsing, never the answers shown."""
    text = "intro\n\n=== ENSEMBLE (2 models answered) ===\n  the answer quoted this block"
    assert consult._substance(text) == "intro"


# --------------------------------------------------------------------------- agents.decision_parsing

_KNOWN_OBJECTS = (
    {"tool": "file_read", "args": {"path": "a/b.txt", "limit": 5}},
    {"final": "done } { \" \\ ``` <think>", "ok": True, "n": None, "f": 1.5},
    {"a": [1, 2, {"b": "}"}], "c": {"d": {"e": []}}, "u": "caf\u00e9 \u2028 \U0001f600"},
    {"x": "\\\"}{\\\\", "y": "{\"nested\": \"json\"}"},
)
_AGENT_SEEDS = tuple(json.dumps(o) for o in _KNOWN_OBJECTS) + (
    "```json\n" + json.dumps(_KNOWN_OBJECTS[0]) + "\n```", 'I think {"tool": "x", "args": {}} is right.', "no json at all", "")
_AGENT_FRAGMENTS = ("{", "}", '"', "\\", "```", "```json\n", "<think>", "</think>", "[", "]", "\x00", "\n", "1e999", "NaN",
                    '{"a": 1}', "{" * 40, "[" * 40, '"}' * 5, "\\\\\\")


def test_extract_agent_json_fuzz_contract_and_roundtrip():
    run = _Run()
    for text in _fuzz_inputs(19, _AGENT_SEEDS, _AGENT_FRAGMENTS):
        returned, value = run.call(extract_agent_json, (text,), (ValueError,))
        if returned:
            try:
                dumped = json.dumps(value)
                if json.dumps(json.loads(dumped)) != dumped:
                    run.failures.append("value does not round-trip: %.100r" % (value,))
            except (TypeError, ValueError) as exc:
                run.failures.append("returned a value json.dumps rejects (%s) for %.150r" % (exc, text))
    for value in (None, "", " \n", 0, [], b"{}"):
        run.call(extract_agent_json, (value if isinstance(value, str) or not value else str(value),), (ValueError,))
    run.check("extract_agent_json")


def test_extract_agent_json_recovers_known_object_from_random_context():
    rng = _rng(20)
    for index in range(ITER * 2):
        obj = rng.choice(_KNOWN_OBJECTS)
        body = json.dumps(obj, indent=rng.choice((None, 1, 2)), ensure_ascii=rng.random() < 0.5)
        lead = rng.choice(("", "```json\n", "```\n", "<think>%s</think>\n" % _prose(rng), "Sure! Here is the decision:\n",
                           "```JSON\n", "\ufeff" if False else ""))
        lead = _prose(rng) + ("\n" if rng.random() < 0.5 else " ") + lead if rng.random() < 0.6 else lead
        trail = rng.choice(("", "\n```", "\nHope that helps!", "\n} extra {", " }}}", "\n{\"second\": 1}", '\n"unterminated', "\n```\n{"))
        text = lead + body + trail
        assert extract_agent_json(text) == obj, "seed %d iter %d: %r" % (SEED, index, text)


def test_extract_agent_json_unbalanced_and_truncated_raise_value_error():
    rng = _rng(21)
    for _ in range(ITER):
        obj = json.dumps(rng.choice(_KNOWN_OBJECTS))
        cut = rng.randrange(1, len(obj))
        text = _prose(rng) + obj[:cut]
        try:
            value = extract_agent_json(text)
        except ValueError:
            continue
        assert json.loads(json.dumps(value)) == value  # a prefix may itself be complete JSON (for example "{}"); that is fine


def _hostile_blob_test(label: str, fn, allowed: tuple, extra: dict[str, str] | None = None) -> None:
    run = _Run()
    blobs = _blobs()
    blobs.update(extra or {})
    for name, blob in blobs.items():
        before = len(run.failures)
        run.call(fn, (blob,), allowed, budget=BLOB_BUDGET)
        for index in range(before, len(run.failures)):
            run.failures[index] = "[%s] %s" % (name, run.failures[index][:200])
    assert not run.failures, "%s: %s" % (label, _where(run.failures))


def test_extract_agent_json_hostile_blobs_stay_in_budget_and_typed():
    _hostile_blob_test("extract_agent_json", extract_agent_json, (ValueError,))


def test_extract_agent_json_deeply_nested_object_regression():
    """Deep nesting makes json.loads raise RecursionError; it must surface as a ValueError."""
    for text in ('{"a":' * 3000, "[" * 3000, '{"a":' * 3000 + "1" + "}" * 3000):
        try:
            extract_agent_json(text)
        except ValueError:
            pass


# --------------------------------------------------------------------------- agents.tool_args

_ARG_KEYS = ("path", "file", "filename", "filepath", "file_path", "query", "pattern", "needle", "text", "content", "body", "old",
             "old_string", "new", "new_string", "program", "args", "cmd", "command", "limit", "flag", "token", "approval",
             "extra_roots", "tool", "name", "arguments", "code", "script", "unknown", "")
_SCHEMAS = (
    None,
    {},
    {"properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
    {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                                        "flag": {"type": "boolean"}, "program": {}, "args": {}, "query": {}, "content": {}}},
    {"type": "object", "properties": {"text": {}, "cmd": {}, "command": {}, "token": {}}},
)


def _arg_value(rng: random.Random, depth: int = 0):
    kind = rng.randrange(14 if depth < 2 else 9)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice((True, False))
    if kind == 2:
        return rng.choice((0, 1, -5, 50, 101, 10**30))
    if kind == 3:
        return rng.choice((0.5, -1.5, 1e300, 99.5))
    if kind in (4, 5):
        return rng.choice(("", "x", " true ", "FALSE", "a/b.c", "ls -la \"my dir\" 'x y'", "echo 'unterminated", "```py\nprint(1)\n```",
                           "```\n```", "```\nouter\n```x```", "```a```", "\\\\server\\x", "[1, 2", "{bad", '{"path": "p"}',
                           '{"tool": "t", "arguments": {"path": "q"}}', "  ", "```json\n{\"a\": 1}\n```"))
    if kind in (6, 7):
        return rng.choice(("true", "false", "7", "  [", '[1,2]', "x" * 50))
    if kind in (8, 9):
        return [_arg_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return {rng.choice(_ARG_KEYS): _arg_value(rng, depth + 1) for _ in range(rng.randint(0, 3))}


def _arg_object(rng: random.Random) -> object:
    form = rng.randrange(10)
    if form == 0:
        return _arg_value(rng)
    obj = {rng.choice(_ARG_KEYS): _arg_value(rng) for _ in range(rng.randint(0, 6))}
    if form == 1:
        return json.dumps(obj)
    if form == 2 and obj:
        return {"tool": "x", "arguments": obj}
    if form == 3:
        return {"name": "x", "arguments": json.dumps(obj)}
    if form == 4:
        return {"args": obj}
    return obj


def test_normalize_tool_args_never_raises_never_mutates_and_returns_pair():
    rng = _rng(22)
    run = _Run()
    for _ in range(ITER * 2):
        args = _arg_object(rng)
        schema = rng.choice(_SCHEMAS)
        tool = rng.choice((None, "file_find", "file_edit", "run", ""))
        args_before, schema_before = copy.deepcopy(args), copy.deepcopy(schema)
        returned, out = run.call(normalize_tool_args, (tool, args, schema), ())
        if returned is None:
            continue
        if args != args_before or schema != schema_before:
            run.failures.append("mutated its input: %.200r -> %.200r" % (args_before, args))
        if not (isinstance(out, tuple) and len(out) == 2 and isinstance(out[1], list) and all(isinstance(n, str) for n in out[1])):
            run.failures.append("result is not (value, [notes]): %.200r" % (out,))
            continue
        value = out[0]
        if isinstance(_decoded(args), dict):
            if not isinstance(value, dict):
                run.failures.append("object args came back as %s: %.200r" % (type(value).__name__, args))
            elif value is args:
                run.failures.append("returned the caller's own dict")
        elif value is not _decoded(args) and value != _decoded(args):
            run.failures.append("non-object args were altered instead of passed through: %.100r -> %.100r" % (args, value))
    run.check("normalize_tool_args")


def _decoded(args: object) -> object:
    if isinstance(args, str):
        text = args.strip()
        if text[:1] in ("[", "{"):
            try:
                return json.loads(text)
            except ValueError:
                return args
    return args


def test_normalize_tool_args_keeps_authority_keys_visible_to_host_policy():
    rng = _rng(23)
    for index in range(ITER):
        key = rng.choice(("token", "approval", "extra_roots"))
        hostile = rng.choice(("grant", True, ["C:\\"], {"x": 1}, 1))
        args = {rng.choice(("path", "query", "other")): "v", key: hostile}
        schema = rng.choice((None, {"properties": {"path": {}}}, {"type": "object", "properties": {"path": {"type": "string"}}}))
        out, _ = normalize_tool_args("file_read", copy.deepcopy(args), schema)
        assert out.get(key) == hostile, "seed %d iter %d: authority key %s dropped from %r" % (SEED, index, key, args)
        wrapped, _ = normalize_tool_args("file_read", {"tool": "file_read", "arguments": args}, schema)
        assert wrapped.get(key) == hostile, "authority key lost inside the envelope: %r" % (args,)


def test_normalize_tool_args_canonical_key_beats_alias():
    rng = _rng(24)
    aliases = {"file": "path", "filename": "path", "filepath": "path", "file_path": "path", "pattern": "query",
               "needle": "query", "old_string": "old", "new_string": "new", "body": "content"}
    for _ in range(ITER):
        alias = rng.choice(list(aliases))
        canonical = aliases[alias]
        items = [(canonical, "CANON"), (alias, "ALIAS")]
        rng.shuffle(items)
        out, _notes = normalize_tool_args(None, dict(items), None)
        assert out[canonical] == "CANON", "alias overrode the canonical key for %r" % (items,)
        only, _ = normalize_tool_args(None, {alias: "ALIAS"}, None)
        assert only == {canonical: "ALIAS"}


def test_normalize_tool_args_is_idempotent_on_flat_objects():
    rng = _rng(25)
    run = _Run()
    keys = tuple(k for k in _ARG_KEYS if k not in ("tool", "name", "arguments", "args", "text"))  # "text": see test_by_design_normalize_tool_args_text_alias_collision
    for _ in range(ITER * 2):
        args = {rng.choice(keys): _arg_value(rng, 2) for _ in range(rng.randint(0, 5))}
        schema = rng.choice(_SCHEMAS)
        returned, first = run.call(normalize_tool_args, (None, args, schema), ())
        if returned is None:
            continue
        returned, second = run.call(normalize_tool_args, (None, first[0], schema), ())
        if returned is not None and second[0] != first[0]:
            run.failures.append("not idempotent: %.200r -> %.200r -> %.200r" % (args, first[0], second[0]))
    run.check("normalize_tool_args idempotence", latency=False)


def test_by_design_normalize_tool_args_text_alias_collision():
    """Normalization is applied once per call in production; a second application can unwrap or drop further.

    Pins the single application: 'needle' becomes 'query' and 'text' stays. Applying it again sees 'query'
    and drops 'text', which is why 'text' is not in the flat-object idempotence key pool above.
    """
    first = normalize_tool_args(None, {"needle": "n", "text": "t"}, None)[0]
    assert first == {"query": "n", "text": "t"}


def test_by_design_normalize_tool_args_nested_envelope():
    """Normalization is applied once per call in production; a second application can unwrap or drop further.

    Pins the single application: only the outer envelope is unwrapped, leaving the inner one in place.
    """
    args = {"tool": "a", "arguments": {"tool": "b", "arguments": {"path": "p"}}}
    out, notes = normalize_tool_args(None, args, None)
    assert out == {"tool": "b", "arguments": {"path": "p"}}
    assert notes == ["unwrapped arguments envelope"]


def test_normalize_decision_aliases_contract():
    rng = _rng(26)
    run = _Run()
    for _ in range(ITER * 2):
        decision = rng.choice((_arg_object(rng), {"name": "t", "arguments": _arg_value(rng, 1)},
                               {"name": rng.choice(("t", "", None)), "tool": rng.choice(("", None, "u")), "arguments": {"p": 1}, "args": {"q": 2}},
                               {"tool": "t", "args": _arg_value(rng, 1)}, {"final": "x"}))
        before = copy.deepcopy(decision)
        returned, out = run.call(normalize_decision_aliases, (decision,), ())
        if returned is None:
            continue
        if decision != before:
            run.failures.append("mutated its input: %.200r" % (before,))
        if isinstance(decision, dict):
            if not isinstance(out, dict) or out is decision:
                run.failures.append("not a fresh dict for %.200r" % (before,))
                continue
            if "arguments" in out:
                run.failures.append("'arguments' survived: %.200r -> %.200r" % (before, out))
            if "arguments" in before and "args" not in before and out.get("args") != before["arguments"]:
                run.failures.append("arguments not moved to args: %.200r -> %.200r" % (before, out))
            if "arguments" in before and "name" in before and not before.get("tool") and out.get("tool") != before["name"]:
                run.failures.append("name not promoted to tool: %.200r -> %.200r" % (before, out))
            if "arguments" in before and before.get("tool") and out.get("tool") != before["tool"]:
                run.failures.append("an existing tool was overwritten: %.200r -> %.200r" % (before, out))
            if normalize_decision_aliases(out) != out:
                run.failures.append("not idempotent: %.200r" % (out,))
        elif out is not decision:
            run.failures.append("non-mapping %.100r not passed through" % (decision,))
    run.check("normalize_decision_aliases", latency=False)


# --------------------------------------------------------------------------- json_reply

_REPLY_SCHEMA_OBJECT = {"type": "object", "required": ["a"], "additionalProperties": False,
                        "properties": {"a": {"type": "integer", "minimum": 0, "maximum": 10}}}
_REPLY_SCHEMAS = (
    _REPLY_SCHEMA_OBJECT,
    {"type": "array", "maxItems": 3, "items": {"type": "string", "pattern": "^[a-z]+$", "maxLength": 5}},
    {"anyOf": [{"type": "null"}, {"type": "object", "properties": {"k": {"enum": ["x", 1, None, True]}}}]},
    {},
)
_REPLY_SEEDS = ('{"a": 3}', '```json\n{"a": 10}\n```', '["ab", "c"]', "null", '{"k": "x"}', '{"a": 3.0}', "```\n[]\n```", "{}")
_REPLY_FRAGMENTS = ("{", "}", "[", "]", '"', "```", "NaN", "Infinity", "-Infinity", "1e999", "\x00", "\n", ",", ":", "null", "true",
                    '"a":', "\ufeff", "//", "'")


def test_check_reply_fuzz_returns_status_never_raises():
    run = _Run()
    rng = _rng(27)
    kinds = {"": 0, "invalid_json": 0, "schema_violation": 0}
    for text in _fuzz_inputs(27, _REPLY_SEEDS, _REPLY_FRAGMENTS):
        schema = rng.choice(_REPLY_SCHEMAS)
        returned, result = run.call(check_reply, (text, schema), ())
        if returned is None:
            continue
        if not isinstance(result, ReplyCheck) or result.kind not in kinds:
            run.failures.append("unexpected result %.100r for %.100r" % (result, text))
            continue
        kinds[result.kind] += 1
        if result.ok:
            try:
                json.dumps(result.value)
            except (TypeError, ValueError):
                run.failures.append("ok value not JSON: %.100r" % (result.value,))
            if result.errors:
                run.failures.append("ok result carries errors")
        elif result.value is not None or not result.errors:
            run.failures.append("failed result must carry errors and no value: %.150r" % (result,))
    run.check("check_reply")
    assert all(count > 20 for count in kinds.values()), "outcomes not all exercised: %r" % (kinds,)


def test_check_reply_matches_oracle_for_the_object_schema():
    rng = _rng(28)

    def oracle(value: object) -> bool:
        if not isinstance(value, dict) or set(value) != {"a"}:
            return False
        a = value["a"]
        if isinstance(a, bool) or not isinstance(a, (int, float)):
            return False
        return float(a).is_integer() and 0 <= a <= 10

    for index in range(ITER * 2):
        a = rng.choice((0, 1, 10, 11, -1, 3.0, 3.5, 10.0, 10.5, True, False, None, "3", [3], {"a": 3}, 10**30, 1e300, -0.0))
        value = rng.choice(({"a": a}, {"a": a}, {"a": a, "b": 1}, {"b": a}, {}, [a], a))
        text = json.dumps(value)
        wrap = rng.choice(("%s", " \n%s\n ", "```json\n%s\n```", "```\n%s\n```", "x %s", "%s x", "```json\n%s", "%s\n```"))
        outcome = check_reply(wrap % text, _REPLY_SCHEMA_OBJECT)
        fenced_ok = wrap in ("%s", " \n%s\n ", "```json\n%s\n```", "```\n%s\n```")
        assert outcome.ok == (fenced_ok and oracle(value)), "seed %d iter %d: %r -> %r" % (SEED, index, wrap % text, outcome)
        if not fenced_ok:
            assert outcome.kind == "invalid_json", "prose around the JSON must be invalid_json, got %r" % (outcome,)


def test_check_reply_non_json_constants_are_invalid_json():
    for literal in ("NaN", "Infinity", "-Infinity", '{"a": NaN}', '[Infinity]', '{"a": -Infinity}'):
        assert check_reply(literal, {}).kind == "invalid_json", literal


def test_check_reply_unsupported_schema_keyword_fails_closed():
    rng = _rng(29)
    extras = ({"oneOf": [{}]}, {"allOf": [{}]}, {"not": {}}, {"$ref": "#/x"}, {"format": "email"}, {"uniqueItems": True},
              {"multipleOf": 2}, {"patternProperties": {}}, {"if": {}}, {"dependencies": {}}, {"minLength": "3"}, {"required": "a"},
              {"pattern": "("}, {"type": "date"}, {"enum": 3}, {"properties": []}, {"anyOf": []}, {"items": [{}]})
    for index in range(ITER):
        schema = copy.deepcopy(rng.choice(_REPLY_SCHEMAS[:3]))
        extra = rng.choice(extras)
        target = schema
        if rng.random() < 0.4 and "properties" in schema:
            target = next(iter(schema["properties"].values()))
        target.update(extra)
        reply = rng.choice(_REPLY_SEEDS + ("not json", ""))
        result = check_reply(reply, schema)
        assert result.kind == UNSUPPORTED_SCHEMA, "seed %d iter %d: %r with %r gave %r" % (SEED, index, extra, schema, result)
        assert result.value is None and not result.ok and result.unchecked


def test_check_reply_hostile_blobs_stay_in_budget():
    run = _Run()
    for name, blob in _blobs().items():
        before = len(run.failures)
        run.call(check_reply, (blob, _REPLY_SCHEMA_OBJECT), (), budget=BLOB_BUDGET)
        run.call(check_reply, ("```json\n" + blob + "\n```", {}), (), budget=BLOB_BUDGET)
        for index in range(before, len(run.failures)):
            run.failures[index] = "[%s] %s" % (name, run.failures[index][:200])
    assert not run.failures, _where(run.failures)


# --------------------------------------------------------------------------- computer_use.rules

_MODIFIERS = ("ctrl", "alt", "shift")
_WIN_WORDS = ("win", "windows", "meta", "super", "cmd")
_CHORD_PARTS = ("ctrl", "Ctrl", "CONTROL", "alt", "shift", "win", "WIN", "windows", "meta", "super", "cmd", "lwin", "enter", "return",
                "tab", "esc", "escape", "del", "delete", "f4", "f13", "f1", "s", "S", "r", "0", "z", "space", "pgup", "pgdn", "home",
                "", " ", "\n", "\t", "\x00", "+", "\u212a", "\uff53", "alt ", "c trl", "ctrl\n", "left", "up")
_DANGEROUS_CHORDS = ("win+r", "WIN+R", "Win + R", "meta+r", "super+e", "cmd+space", "windows+d", "w i n + r", "win", "ctrl+win+left",
                     "ctrl+alt+del", "ctrl+alt+delete", "alt+ctrl+del", "del+ctrl+alt", "ctrl+shift+esc", "ctrl+shift+escape",
                     "ctrl+esc", "alt+esc", "alt+tab", "alt+shift+tab", "shift+alt+tab", "TAB+ALT", "alt+space", "ctrl+s\nwin+r",
                     "ctrl+\nwin+r", "ctrl+s+win+r", "ctrl+alt+tab", "ctrl+alt+shift+tab", "shift+alt+ctrl+tab")


def _chord_problem(raw: str, chord: tuple[str, ...]) -> str | None:
    *mods, key = chord
    if not chord or any(m not in _MODIFIERS for m in mods) or len(set(mods)) != len(mods):
        return "modifiers invalid"
    if list(mods) != [m for m in _MODIFIERS if m in mods]:
        return "modifiers not in canonical order"
    if not (key in rules._NAMED_KEYS or re.fullmatch(r"[a-z0-9]", key)):
        return "key %r is not a known key name" % key
    if any(part in _WIN_WORDS for part in chord):
        return "Windows key accepted"
    switches = {(frozenset(("alt",)), "tab"), (frozenset(("alt", "shift")), "tab"), (frozenset(("ctrl",)), "escape"),
                (frozenset(("alt",)), "escape"), (frozenset(("alt", "ctrl")), "delete"), (frozenset(("ctrl", "shift")), "escape"),
                (frozenset(("alt",)), "space"),
                (frozenset(("alt", "ctrl")), "tab"), (frozenset(("alt", "ctrl", "shift")), "tab")}
    if (frozenset(mods), key) in switches:
        return "task-switch chord accepted"
    return None


def test_parse_chord_fuzz_accepts_only_known_keys():
    rng = _rng(30)
    run = _Run()
    accepted = 0
    for _ in range(ITER * 3):
        raw = rng.choice(("", " ", "+")).join(rng.choice(_CHORD_PARTS) for _ in range(rng.randint(0, 5)))
        returned, chord = run.call(rules.parse_chord, (raw,), (rules.ActionRefused,))
        if returned:
            accepted += 1
            problem = _chord_problem(raw, chord)
            if problem:
                run.failures.append("%s: %r -> %r" % (problem, raw, chord))
    for value in (None, 0, [], b"ctrl+s", object(), "x" * 5000, "a+" * 5000):
        run.call(rules.parse_chord, (value,), (rules.ActionRefused,))
    run.check("parse_chord")
    assert accepted > 100, "only %d chords accepted" % accepted


@pytest.mark.parametrize("chord", _DANGEROUS_CHORDS)
def test_parse_chord_refuses_dangerous_chords(chord):
    with pytest.raises(rules.ActionRefused):
        rules.parse_chord(chord)


@pytest.mark.parametrize("raw, expected", [("ctrl+s", ("ctrl", "s")), ("S", ("s",)), ("Shift+Ctrl+Z", ("ctrl", "shift", "z")),
                                           ("esc", ("escape",)), ("return", ("enter",)), (" ctrl + alt + f4 ", ("ctrl", "alt", "f4")),
                                           ("alt+f4", ("alt", "f4")), ("control+a", ("ctrl", "a")), ("pgdn", ("pagedown",)),
                                           ("ctrl+s\n", ("ctrl", "s")), ("F12", ("f12",))])
def test_parse_chord_accepts_documented_chords(raw, expected):
    assert rules.parse_chord(raw) == expected


def test_parse_chord_planted_windows_key_is_refused_in_any_position():
    rng = _rng(31)
    for _ in range(ITER):
        parts = [rng.choice(("ctrl", "alt", "shift", "s", "enter", "f2")) for _ in range(rng.randint(0, 3))]
        parts.insert(rng.randint(0, len(parts)), _case(rng, rng.choice(_WIN_WORDS)))
        raw = rng.choice(("+", " + ", "+ ")).join(parts)
        with pytest.raises(rules.ActionRefused):
            rules.parse_chord(raw)


_STEP_SEEDS = (
    '{"done": false, "action": "click", "x": 500, "y": 250, "label": "OK", "reason": "press it"}',
    'Sure:\n```json\n{"action": "key", "keys": "ctrl+s", "reason": "save"}\n```',
    '{"done": true, "reason": "finished"}',
    '{"action": "type", "text": "hello\\nworld", "label": "box"}',
    '{"action": "scroll", "x": 1, "y": 2, "scroll": -5}',
)
_STEP_FRAGMENTS = ("{", "}", '"', "\\", "\x00", "\n", '"x": 1e999', '"x": NaN', '"scroll": 1e999', '"scroll": "9"', '"action": "Click"',
                   '"keys": "win+r"', '"text": "\\u0000"', "[", "]", "null", '"done": "true"', '"label": "' + "L" * 400 + '"')
_STEP_KEYS = {"done", "action", "x", "y", "text", "keys", "scroll", "label", "reason"}


def test_parse_model_step_fuzz_closed_shape():
    run = _Run()
    for text in _fuzz_inputs(32, _STEP_SEEDS, _STEP_FRAGMENTS):
        returned, step = run.call(rules.parse_model_step, (text,), (rules.ActionRefused,))
        if not returned:
            continue
        problems = []
        if set(step) != _STEP_KEYS:
            problems.append("keys %r" % sorted(step))
        if not isinstance(step.get("done"), bool):
            problems.append("done not bool")
        for name, limit in (("action", 20), ("keys", 40), ("label", rules.MAX_LABEL_CHARS), ("reason", 300)):
            value = step.get(name)
            if not isinstance(value, str) or len(value) > limit or any(ch < " " and ch != "\n" for ch in value) or value != value.strip():
                problems.append("%s not a clean str <= %d: %.60r" % (name, limit, value))
        if not isinstance(step.get("text"), str) or len(step["text"]) > rules.MAX_TYPE_CHARS:
            problems.append("text over limit")
        if problems:
            run.failures.append("%s on %.200r" % ("; ".join(problems), text))
    run.check("parse_model_step")


def test_parse_model_step_never_yields_an_action_outside_the_allowed_set():
    """parse_model_step is a shape check; the allowed set is enforced by build_action, which must hold for every parsed step."""
    run = _Run()
    for text in _fuzz_inputs(33, _STEP_SEEDS, _STEP_FRAGMENTS):
        returned, step = run.call(rules.parse_model_step, (text,), (rules.ActionRefused,))
        if not returned:
            continue
        returned, act = run.call(
            lambda s=step: rules.build_action(s["action"], x=s["x"], y=s["y"], coords="normalized", width=800, height=600,
                                              text=s["text"], keys=s["keys"], scroll=s["scroll"], label=s["label"]),
            (), (rules.ActionRefused,))
        if returned:
            problem = None
            if act.action not in rules.ACTIONS:
                problem = "action %r outside ACTIONS" % act.action
            elif act.action in rules.POINTER_ACTIONS and not (0 <= act.x < 800 and 0 <= act.y < 600):
                problem = "point outside the window"
            elif act.chord and _chord_problem(step["keys"], act.chord):
                problem = "chord %r: %s" % (act.chord, _chord_problem(step["keys"], act.chord))
            elif act.scroll and abs(act.scroll) > rules.MAX_SCROLL_NOTCHES:
                problem = "scroll %d" % act.scroll
            elif any(ch < " " and ch not in "\n\t" for ch in act.text) or len(act.text) > rules.MAX_TYPE_CHARS:
                problem = "text violates the control character or length limit"
            if problem:
                run.failures.append("%s for step %.200r" % (problem, step))
    run.check("parse_model_step -> build_action", latency=False)


@pytest.mark.parametrize("field, value", [("scroll", "1e999"), ("scroll", "Infinity"), ("x", "1" + "0" * 400), ("y", "1" + "0" * 400),
                                          ("x", "1e999"), ("scroll", "1" + "0" * 400)])
def test_build_action_refuses_non_finite_or_huge_numbers_from_a_model_step(field, value):
    """Numbers json.loads accepts but float()/int() cannot hold are refused, not raised as OverflowError."""
    reply = '{"action": "scroll", "x": 5, "y": 5, "label": "l", "%s": %s}' % (field, value)
    step = rules.parse_model_step(reply)
    with pytest.raises(rules.ActionRefused):
        rules.build_action(step["action"], x=step["x"], y=step["y"], coords="normalized", width=800, height=600,
                           text=step["text"], keys=step["keys"], scroll=step["scroll"], label=step["label"])


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), 10**400, -(10**400)])
def test_pixel_coordinates_refuse_non_finite_and_overflowing_numbers(bad):
    """isfinite covers inf/nan; float() raising OverflowError covers 10**400. Both must be ActionRefused."""
    with pytest.raises(rules.ActionRefused):
        rules.to_pixels(bad, 5, "pixels", 800, 600)
    with pytest.raises(rules.ActionRefused):
        rules.to_pixels(5, bad, "pixels", 800, 600)
    with pytest.raises(rules.ActionRefused):
        rules.build_action("click", x=bad, y=5, coords="pixels", width=800, height=600, label="l")


def test_scroll_refuses_non_finite_floats_and_oversized_numbers_each_by_its_own_check():
    for bad in (float("inf"), float("-inf"), float("nan")):  # the isfinite check
        with pytest.raises(rules.ActionRefused, match="integer"):
            rules.build_action("scroll", x=5, y=5, coords="pixels", width=800, height=600, scroll=bad, label="l")
    # 10**400 is an int: isfinite is skipped, int() succeeds and the notch cap refuses it.
    with pytest.raises(rules.ActionRefused):
        rules.build_action("scroll", x=5, y=5, coords="pixels", width=800, height=600, scroll=10**400, label="l")


class _OverflowingInt:
    """An object whose int() conversion raises OverflowError, the way int(float('inf')) does."""

    def __int__(self):
        raise OverflowError("too large")


def test_scroll_overflow_error_from_int_conversion_is_refused():
    with pytest.raises(rules.ActionRefused, match="integer"):
        rules.build_action("scroll", x=5, y=5, coords="pixels", width=800, height=600, scroll=_OverflowingInt(), label="l")


def test_parse_model_step_hostile_blobs_stay_in_budget_and_typed():
    _hostile_blob_test("parse_model_step", rules.parse_model_step, (rules.ActionRefused,))


def test_parse_model_step_deep_nesting_is_refused_not_crashed():
    for text in ('{"a":' * 3000, "{" + "[" * 3000 + "}", '{"a":' + "[" * 3000 + "]" * 3000 + "}"):
        with pytest.raises(rules.ActionRefused):
            rules.parse_model_step(text)


def test_parse_model_step_recovers_planted_object_in_random_context():
    rng = _rng(34)
    for index in range(ITER):
        action = rng.choice(sorted(rules.ACTIONS))
        obj = {"done": False, "action": _case(rng, action), "x": rng.randint(0, 1000), "y": rng.randint(0, 1000), "label": "L"}
        text = _prose(rng) + rng.choice(("", "```json\n")) + json.dumps(obj) + rng.choice(("", "\n```")) + _prose(rng)
        step = rules.parse_model_step(text)
        assert (step["action"], step["x"], step["y"], step["done"]) == (action, obj["x"], obj["y"], False), "seed %d iter %d" % (SEED, index)


def test_parse_model_step_done_only_for_boolean_true():
    for value, expected in (("true", True), ("false", False), ('"true"', False), ("1", False), ("null", False), ('"yes"', False)):
        assert rules.parse_model_step('{"done": %s}' % value)["done"] is expected


def test_normalize_app_contract():
    rng = _rng(35)
    run = _Run()
    names = ("notepad.exe", "Game.EXE", "a b.exe", "", " ", ".", "..", "x.exe.", "x.exe ", "\u0130.exe", "\u212a.exe")
    dirs = ("C:\\Program Files\\", "/usr/bin/", "..\\..\\", "\\\\server\\share\\", "C:/x//", "", "dir\\", "/")
    for _ in range(ITER * 2):
        if rng.random() < 0.2:
            value = rng.choice((None, 0, 1.5, [], b"x.exe", False, object()))
        else:
            value = rng.choice(dirs) + rng.choice(names) + rng.choice(("", " ", "\t", "\n", "\x00"))
        returned, got = run.call(rules.normalize_app, (value,), ())
        if returned is None:
            continue
        if not isinstance(got, str) or "/" in got or "\\" in got or got != got.strip() or got != got.lower():
            run.failures.append("%.80r -> %r is not a bare lower-case file name" % (value, got))
        elif rules.normalize_app(got) != got:
            run.failures.append("not idempotent: %.80r -> %r -> %r" % (value, got, rules.normalize_app(got)))
        elif isinstance(value, str) and got and value.replace("\\", "/").rsplit("/", 1)[-1].strip().lower() != got:
            run.failures.append("%.80r -> %r does not keep the final path component" % (value, got))
    run.check("normalize_app", latency=False)
    assert rules.normalize_app("C:\\Program Files\\X\\Game.EXE") == "game.exe"
    for blank in ("", "dir/", "C:\\dir\\", None, "  "):
        assert rules.app_allowed(blank, ("", "x.exe", None)) is False, "%r must never be allowed" % (blank,)
    assert rules.app_allowed("D:\\g\\NOTEPAD.exe", ["notepad.exe"]) is True
    assert rules.app_allowed("notepad.exe.", ["notepad.exe"]) is False


# --------------------------------------------------------------------------- meshgen.critique

_CRITIQUE_SEEDS = (
    '{"score": 7, "recognizable": true, "issues": ["wheels lie flat; rotate 90 degrees"]}',
    '```json\n{"score": "6", "issues": "one issue"}\n```',
    'prose {"score": 4.5, "recognizable": false, "issues": []} more',
    '{"score": 99, "issues": ["a", "", "  ", 3, null, {"x": 1}]}',
    "no json",
)
_CRITIQUE_FRAGMENTS = ("{", "}", '"', "\\", "[", "]", "NaN", "Infinity", "1e999", "-1e999", '"score": null', '"score": true', '"score": "x"',
                       '"issues": ' + str(["i"] * 30).replace("'", '"'), "\x00", "\n", '{"score": 3}', "<think>", "```")


def _critique_problem(out: object) -> str | None:
    if not isinstance(out, dict):
        return "not a dict"
    if out.get("score") is None:
        return None if (out.get("issues") == [] and isinstance(out.get("raw"), str) and len(out["raw"]) <= 400) else "malformed failure dict"
    if set(out) != {"score", "recognizable", "issues"}:
        return "keys %r" % sorted(out)
    if isinstance(out["score"], bool) or not isinstance(out["score"], int) or not 0 <= out["score"] <= 10:
        return "score %r" % (out["score"],)
    if not isinstance(out["recognizable"], bool):
        return "recognizable not bool"
    issues = out["issues"]
    if not isinstance(issues, list) or len(issues) > MAX_ISSUES or any(
            not isinstance(i, str) or not i.strip() or len(i) > 300 or i != i.strip() for i in issues):
        return "issues invalid"
    return None


def test_parse_critique_fuzz_contract():
    run = _Run()
    tolerated = 0
    for text in _fuzz_inputs(36, _CRITIQUE_SEEDS, _CRITIQUE_FRAGMENTS):
        returned, out = run.call(parse_critique, (text,), ())
        if returned is None:
            continue
        problem = _critique_problem(out)
        if problem:
            run.failures.append("%s on %.200r" % (problem, text))
        elif out["score"] is not None:
            tolerated += 1
    for value in (None, 0, [], b"{}", object(), "", " "):
        run.call(parse_critique, (value,), ())
    run.check("parse_critique")
    assert tolerated > 100, "only %d critiques parsed" % tolerated


def test_parse_critique_recovers_planted_object_in_random_context():
    rng = _rng(37)
    for index in range(ITER):
        raw_score = rng.choice((0, 1, 5, 10, 11, 99, -3, 4.4, 4.5, 5.5, 6.5, "7", " 7 ", "7.6", 1e3, "-2"))
        issues = [rng.choice(("fix a", "  fix b  ", "x" * 400, "")) for _ in range(rng.randint(0, 12))]
        obj = {"score": raw_score, "issues": issues}
        text = _prose(rng) + rng.choice(("", "```json\n", "{not json} ", "[1, 2] ")) + json.dumps(obj) + rng.choice(("", "\n```", " }"))
        out = parse_critique(text)
        expected = max(0, min(10, round(float(raw_score))))
        assert out["score"] == expected, "seed %d iter %d: %r -> %r" % (SEED, index, text, out)
        assert out["issues"] == [i.strip()[:300] for i in issues if i.strip()][:MAX_ISSUES]
        assert out["recognizable"] is (expected >= 5)


def test_parse_critique_unusable_scores_give_the_failure_shape():
    for score in ("true", "null", '"x"', "[]", "{}", '"nan"', '"inf"', '"1e999"', "1e999", "-1e999", "NaN", "Infinity", '""'):
        out = parse_critique('{"score": %s, "issues": ["a"]}' % score)
        assert out["score"] is None and out["issues"] == [] and "raw" in out, "score %s gave %r" % (score, out)
    assert parse_critique(None) == {"score": None, "issues": [], "raw": ""}
    assert len(parse_critique("x" * 1000)["raw"]) == 400


def test_parse_critique_hostile_blobs_stay_in_budget():
    _hostile_blob_test("parse_critique", parse_critique, ())


def test_parse_critique_deep_nesting_returns_failure_shape_regression():
    """A deeply nested reply returns the failure shape instead of raising RecursionError."""
    for text in ('{"a":' * 3000, '{"score": 5, "x": ' + "[" * 3000):
        out = parse_critique(text)
        assert out["score"] is None and out["issues"] == [] and out["raw"] == text[:400]


# --------------------------------------------------------------------------- verdict hostile blobs

_THINK_BLOBS = {
    "early-closer-then-openers": "<think></think>x" + "<think>" * BLOB,
    "closed-blocks-then-opener": "<think>a</think>" * (BLOB // 16) + "<think>",
    "closer-spam": "</think>" * (BLOB // 8) + "<think>",
    "upper-case-openers": "<THINK></THINK>x" + "<THINK>" * BLOB,
}


def test_parse_verdict_hostile_blobs_stay_in_budget_and_typed():
    _hostile_blob_test("parse_verdict", parse_verdict, (VerdictParseError,), _THINK_BLOBS)


def test_parse_hypotheses_hostile_blobs_stay_in_budget_and_typed():
    _hostile_blob_test("parse_hypotheses", parse_hypotheses, (VerdictParseError,), _THINK_BLOBS)


def test_think_blocks_are_stripped_case_insensitively_and_unterminated_ones_drop_the_rest():
    assert parse_verdict('<THINK>{"verdict": "REFUTED"}</Think>{"verdict": "CONFIRMED"}').verdict == "CONFIRMED"
    assert parse_verdict('<think>a</think>{"verdict": "CONFIRMED"}<think>{"verdict": "REFUTED"}').verdict == "CONFIRMED"
    assert parse_verdict('<think></think>{"verdict": "UNCERTAIN"}' + "<think>" * 5).verdict == "UNCERTAIN"
    with pytest.raises(VerdictParseError):
        parse_verdict('<think>{"verdict": "REFUTED"}')


_DOTTED_I = "İ"  # str.lower() turns it into two characters, which shifted indices into the original text


def test_think_stripping_stays_index_aligned_after_dotted_capital_i():
    hidden = '<think>{"verdict":"CONFIRMED"}</think>\nI could not decide.'
    with pytest.raises(VerdictParseError):
        parse_verdict(_DOTTED_I * 40 + hidden)
    assert parse_hypotheses(_DOTTED_I * 80 + '<think>[{"hypothesis": "h"}]</think>\n[]') == ()
    turkish = ('İSTANBUL İZMİR İĞDIR raporu. <think>Belki {"verdict":"CONFIRMED"} olabilir '
               'ama emin değilim</think>\nVERDICT: REFUTED')
    assert parse_verdict(turkish).verdict == "REFUTED"
    # an unterminated opener after the dotted capitals still drops the rest, and nothing before it
    with pytest.raises(VerdictParseError):
        parse_verdict(_DOTTED_I * 40 + '<think>{"verdict":"CONFIRMED"}')
    assert parse_verdict('{"verdict": "REFUTED"}' + _DOTTED_I * 40 + '<think>{"verdict":"CONFIRMED"}').verdict == "REFUTED"
    assert parse_verdict(_DOTTED_I * 40 + '{"verdict": "UNCERTAIN"}<THINK>x</Think>').verdict == "UNCERTAIN"


_REFERENCE_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_REFERENCE_OPEN_RE = re.compile(r"<think>", re.IGNORECASE)


def _reference_strip_think(text: str) -> str:
    """The base implementation (regex substitution, then cut at an unterminated opener), index-aligned."""
    text = _REFERENCE_THINK_RE.sub("", text)
    opener = _REFERENCE_OPEN_RE.search(text)
    return text if opener is None else text[:opener.start()]


def test_strip_think_matches_the_regex_reference_on_random_fragment_strings():
    from sonder_runtime.domain.security_eval.verdict import _strip_think

    fragments = ("<think>", "</think>", "<THINK>", "</Think>", "<thinK>", _DOTTED_I, "K", "x",
                 '{"verdict":"CONFIRMED"}', "\n")
    rng = random.Random(SEED)
    for _ in range(2000):
        text = "".join(rng.choice(fragments) for _ in range(rng.randint(0, 24)))
        assert _strip_think(text) == _reference_strip_think(text), repr(text)


def test_strip_think_long_opener_run_after_a_closed_block_is_linear():
    from sonder_runtime.domain.security_eval.verdict import _strip_think

    text = "<think></think>x" + "<think>" * 200_000
    started = time.perf_counter()
    assert _strip_think(text) == "x"
    assert time.perf_counter() - started < BLOB_BUDGET


def test_scanner_skips_implausible_openers_without_spending_the_failure_cap():
    code = "int f() {\n" + "  { count++; }\n" * 200 + "}\n"
    assert parse_verdict(code + '{"verdict": "REFUTED", "reason": "guarded"}').verdict == "REFUTED"
    assert parse_critique(code + '{"score": 7, "issues": []}')["score"] == 7
    assert parse_hypotheses("[i] " * 200 + '[{"hypothesis": "h"}]')[0].hypothesis == "h"
    # a truncated outer object still leaves the inner one findable
    assert parse_verdict('{"result": {"verdict": "REFUTED"}').verdict == "REFUTED"
    assert parse_critique('{"result": {"score": 4, "issues": []}')["score"] == 4


def test_scanner_opener_runs_stay_in_budget():
    for blob in ("{" * BLOB, "{ " * (BLOB // 2), '{"' * (BLOB // 2), "[" * BLOB, "[ " * (BLOB // 2)):
        for fn in (parse_verdict, parse_hypotheses, parse_critique):
            started = time.perf_counter()
            try:
                fn(blob)
            except VerdictParseError:
                pass
            assert time.perf_counter() - started < BLOB_BUDGET, "%s took too long on %.12r" % (fn.__name__, blob)


def test_parse_verdict_and_hypotheses_deep_nesting_regression():
    """Deep JSON nesting raises RecursionError inside raw_decode; the scanner contains it."""
    for text in ('{"a":' * 3000, "[" * 3000):
        for fn in (parse_verdict, parse_hypotheses):
            with pytest.raises(VerdictParseError):
                fn(text)
    assert parse_verdict('{"verdict": "CONFIRMED"} ' + "[" * 3000).verdict == "CONFIRMED"
    assert parse_hypotheses('[{"hypothesis": "h"}] ' + "[" * 3000)[0].hypothesis == "h"


def test_consult_helpers_hostile_blobs_stay_in_budget():
    run = _Run()
    stacked = "\n=== ENSEMBLE (1 model answered) ===\n  m 0ms" * 20_000
    blobs = {**_blobs(), "stacked-footers": "answer" + stacked, "stacked-footers-near-miss": "answer" + stacked + "x",
             "stacked-footers-then-line": "answer" + stacked + "\nx"}
    for name, blob in blobs.items():
        before = len(run.failures)
        for fn in (consult._judge_verdict, consult._substance):
            run.call(fn, (blob,), (), budget=BLOB_BUDGET)
        run.call(rules.parse_chord, (blob,), (rules.ActionRefused,), budget=BLOB_BUDGET)
        run.call(rules.normalize_app, (blob,), (), budget=BLOB_BUDGET)
        for index in range(before, len(run.failures)):
            run.failures[index] = "[%s] %s" % (name, run.failures[index][:200])
    assert not run.failures, _where(run.failures)
