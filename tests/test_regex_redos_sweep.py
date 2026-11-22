"""Adversarial-input timing and equivalence checks for polynomial-regex fixes."""
import functools
import re
import time

import pytest

import intents
from sonder_runtime.application.chat import tool_intent
from sonder_runtime.domain import batch_jobs, fanout_redaction
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.memory import write_quality
from sonder_runtime.domain.meshgen import prompts as meshgen_prompts
from sonder_runtime.domain.testing import report_parsers

BUDGET_SECONDS = 0.75
BACKSLASH = chr(92)
LIMIT_SECONDS = 2.0
FAST_SECONDS = 0.5


def _elapsed(call):
    started = time.perf_counter()
    result = call()
    return time.perf_counter() - started, result


def test_batch_placeholder_unclosed_whitespace_is_linear():
    text = "{{" + " " * 4000
    seconds, refs = _elapsed(lambda: batch_jobs.template_references(text))
    assert refs == ((), ())
    assert seconds < BUDGET_SECONDS
    seconds, _ = _elapsed(lambda: list(batch_jobs._PLACEHOLDER.finditer("{{ " * 4000)))
    assert seconds < BUDGET_SECONDS


@pytest.mark.parametrize("template, fields, stages", [
    ("hello {{name}} and {{ other_field }}", ("name", "other_field"), ()),
    ("{{\t stage.one \n}} {{stage.two}} {{ stage.one }}", (), ("one", "two")),
    ("{{ a }} {{a}} {{  b  }}", ("a", "b"), ()),
    ("no placeholders { here } {{", (), ()),
])
def test_batch_template_references_unchanged(template, fields, stages):
    assert batch_jobs.template_references(template) == (fields, stages)


@pytest.mark.parametrize("template", ["{{}}", "{{   }}", "x {{ bad name }} y", "{{ stage. }}"])
def test_batch_template_references_still_rejects_malformed(template):
    with pytest.raises(InvalidInput):
        batch_jobs.template_references(template)


def test_batch_placeholder_matches_same_tokens_as_before():
    old = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
    for sample in ["{{a}}", "{{ a b }}", "{{\n a \t}}", "{{}}", "{{ }}", "{{ {{a}} }}", "{{a}}{{b}}",
                   "x{{  stage.s  }}y", "{{ a }}", "{{ a { }}"]:
        got = [m.group(1).strip() for m in batch_jobs._PLACEHOLDER.finditer(sample)]
        assert got == [m.group(1) for m in old.finditer(sample)], sample


def test_write_quality_sentence_break_dot_run_is_linear():
    seconds, _ = _elapsed(lambda: write_quality._SENTENCE_BREAK.split("." * 20000))
    assert seconds < BUDGET_SECONDS
    seconds, _ = _elapsed(lambda: write_quality.classify("." * 20000))
    assert seconds < BUDGET_SECONDS


def test_write_quality_sentence_split_unchanged():
    old = re.compile(r"[.!?]+[\"')\]]*\s+(?=[A-Z0-9\"'(\[])")
    for sample in ["Use X. It avoids Y.", "Really?! Yes... Then go.", "He said (hi.) Next 5 things.",
                   "a.b is fine. But 'this' is not!  \"Quote\" follows.", "no break here", "End.\nNew 1 line.",
                   "Wait... what?) Ok [x]. [Y] done."]:
        assert write_quality._SENTENCE_BREAK.split(sample) == old.split(sample), sample


def test_tool_intent_suppresses_file_tools_dotted_input_is_bounded():
    seconds, _ = _elapsed(lambda: tool_intent.suppresses_file_tools("a." * 10000))
    assert seconds < BUDGET_SECONDS
    seconds, _ = _elapsed(lambda: tool_intent._LOCAL_TARGET.search("a." * 6000))
    assert seconds < BUDGET_SECONDS
    seconds, _ = _elapsed(lambda: tool_intent._EXPLICIT_FILE.search("a." * 6000))
    assert seconds < BUDGET_SECONDS


@pytest.mark.parametrize("negation", [
    "do not edit any file",
    "never write anything",
    "do not use tools on the file",
])
def test_long_prompt_with_negation_is_still_suppressed(negation):
    filler = " lorem ipsum dolor sit amet" * 600
    prefix = "use ensemble code and reasoning with compiler feedback retries to review src/parse.py but "
    text = prefix + negation + "." + filler
    assert len(text) > 12000
    assert intents.classify_execution(text) is None
    assert intents.classify_execution(prefix + filler + " " + negation) is None
    assert tool_intent.suppresses_file_tools("explain the file: " + negation + filler) is True


@pytest.mark.parametrize("text", [
    "Don't use tools, just explain what main.py does",
    "no tools: tell me about the README",
    "explain the file layout without running anything",
])
def test_tool_intent_suppression_unchanged(text):
    assert tool_intent.suppresses_file_tools(text) is True


def test_tool_intent_file_names_still_recognised():
    assert tool_intent._LOCAL_TARGET.search("see notes.txt now")
    assert tool_intent._EXPLICIT_FILE.search("open server.py")
    assert tool_intent._EXPLICIT_FILE.search("open a.c")


def test_report_parser_generic_location_dot_run_is_bounded():
    seconds, _ = _elapsed(lambda: report_parsers._GENERIC_LOCATION.search("." * 20000))
    assert seconds < BUDGET_SECONDS


@pytest.mark.parametrize("text, file, line", [
    ("boom at src/app/main.rs:42", "src/app/main.rs", "42"),
    ("see C:" + BACKSLASH + "proj" + BACKSLASH + "x.cs:line 7",
     BACKSLASH + "proj" + BACKSLASH + "x.cs", "7"),
    ("tests/a_b.test.ts:9", "tests/a_b.test.ts", "9"),
])
def test_report_parser_generic_location_unchanged(text, file, line):
    match = report_parsers._GENERIC_LOCATION.search(text)
    assert (match.group("file"), match.group("line")) == (file, line)


def test_fanout_credential_check_no_digit_is_linear():
    fragment = "a" * 20000
    seconds, result = _elapsed(lambda: fanout_redaction._has_compact_credential(fragment))
    assert result is False
    assert seconds < BUDGET_SECONDS
    seconds, _ = _elapsed(lambda: fanout_redaction._has_compact_credential("ab\n" * 20000))
    assert seconds < BUDGET_SECONDS


def test_fanout_credential_check_matches_original_pattern():
    old = re.compile(r"(?=.*\d)[A-Za-z0-9_./:+-]{8,}")
    for sample in ["abcdefgh1", "abcdefgh 1", "1 abcdefgh", "abcdefgh\n1", "abc1", "short 1 abcdefgh",
                   "abcdefghij", "key=sk-12345678", "ab cd\nabcdefgh9\nx", "", "1234567", "12345678",
                   "abcdefg 1 abcdefg", "x\nabcdefgh", "a.b/c:d+e-f_g2"]:
        assert fanout_redaction._has_compact_credential(sample) == bool(old.search(sample)), repr(sample)


def test_meshgen_qualified_unavailable_is_bounded():
    seconds, _ = _elapsed(lambda: meshgen_prompts._QUALIFIED_UNAVAILABLE.search("a_" * 10000))
    assert seconds < BUDGET_SECONDS
    seconds, _ = _elapsed(lambda: meshgen_prompts._QUALIFIED_UNAVAILABLE.search("a." * 10000))
    assert seconds < BUDGET_SECONDS


def test_meshgen_qualified_unavailable_still_matches():
    match = meshgen_prompts._QUALIFIED_UNAVAILABLE.search("NameError: kit.foo is not available in build scripts")
    assert (match.group(1), match.group(2)) == ("kit", "foo")


def _best_of_three(call):
    return max(min(_elapsed(call)[0] for _ in range(3)), 0.005)


def assert_linear(make_input, run, base=25_000):
    """Growth-ratio check: t(4N)/t(N) is near 4 when linear and near 16 when quadratic."""
    small = make_input(base)
    small_time = _best_of_three(lambda: run(small))
    assert small_time < FAST_SECONDS
    large_time = _best_of_three(lambda: run(make_input(base * 4)))
    assert large_time / small_time < 8
    assert large_time < LIMIT_SECONDS * 4


@pytest.mark.parametrize("unit", ["A-", "A+", "A#", "Ab-"])
def test_write_quality_named_version_run_is_linear(unit):
    assert_linear(lambda n: unit * (n // len(unit)),
                  lambda text: list(write_quality._NAMED_VERSION.finditer(text)))
    assert _elapsed(lambda: write_quality.classify(unit * 5000))[0] < LIMIT_SECONDS


@pytest.mark.parametrize("text, name", [
    ("We use Python 3.12 daily", "Python"),
    ("C++ 20.1 is out", "C++"),
    ("C# 12.0 shipped", "C#"),
    ("Node-RED 3.1 works", "Node-RED"),
    ("UE 5.8 is current", "UE"),
])
def test_write_quality_named_version_still_matches_products(text, name):
    assert [m.group(1) for m in write_quality._NAMED_VERSION.finditer(text)] == [name]
    assert write_quality.is_time_sensitive(text) is True


@pytest.mark.parametrize("text", ["Timeout 2.5 seconds", "Section 3.2", "Wait 1.5 s"])
def test_write_quality_named_version_exclusions_unchanged(text):
    assert write_quality.is_time_sensitive(text) is False


def test_trx_location_run_is_linear():
    assert_linear(lambda n: "in " * (n // 3), report_parsers._trx_location)
    assert_linear(lambda n: "at x in " * (n // 8), report_parsers._trx_location)
    assert_linear(lambda n: "in " * (n // 3) + ":line", report_parsers._trx_location)


@pytest.mark.parametrize("stack, expected", [
    ("at X.Y() in C:" + BACKSLASH + "src" + BACKSLASH + "a.cs:line 42",
     ("C:" + BACKSLASH + "src" + BACKSLASH + "a.cs", "42")),
    ("at X.Y() in /home/u/a.cs:line 7", ("/home/u/a.cs", "7")),
    ("at A() in /x/a.cs:line 1\n at B() in /y/b.cs:line 2", ("/x/a.cs", "1")),
    ("noise in a in b:line 3", ("a in b", "3")),
    ("in :line 5 then in /p/q.cs:line 6", (":line 5 then in /p/q.cs", "6")),
    ("my path in c:line 9 :line 10", ("c", "9")),
])
def test_trx_location_matches_original_pattern(stack, expected):
    old = re.compile(r"in (?P<file>[^\n]+?):line (?P<line>\d+)").search(stack)
    assert (old.group("file"), old.group("line")) == expected
    assert report_parsers._trx_location(stack) == (expected[0], int(expected[1]))


@pytest.mark.parametrize("stack", ["", "no location here", "in x:line", "in :line 4", "in\nfoo:line 3"])
def test_trx_location_absent(stack):
    assert report_parsers._trx_location(stack) is None


def test_trx_document_with_long_stack_trace_parses_fast():
    stack = "in " * 40_000
    doc = (
        '<?xml version="1.0"?><TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">'
        '<Results><UnitTestResult testName="t" outcome="Failed"><Output><ErrorInfo>'
        "<Message>boom</Message><StackTrace>" + stack + "\nat X() in /a/b.cs:line 9</StackTrace>"
        "</ErrorInfo></Output></UnitTestResult></Results></TestRun>"
    )
    seconds, parsed = _elapsed(lambda: report_parsers.parse_trx(doc.encode()))
    assert seconds < LIMIT_SECONDS
    assert parsed.failures[0].file.endswith("b.cs")


def test_libtest_panic_old_run_is_linear():
    assert_linear(lambda n: "panicked at '" * (n // 13), report_parsers._libtest_panic_old)
    assert_linear(lambda n: "panicked at '" * (n // 13) + "', ", report_parsers._libtest_panic_old)


@pytest.mark.parametrize("line, expected", [
    ("thread 'x' panicked at 'boom', src/lib.rs:10:5", ("boom", "src/lib.rs", "10")),
    ("panicked at 'a', b', c', src/x.rs:1:2", ("a', b', c", "src/x.rs", "1")),
    ("panicked at 'junk panicked at 'real', f.rs:3:4", ("junk panicked at 'real", "f.rs", "3")),
    ("panicked at '', f.rs:3:4", ("", "f.rs", "3")),
    ("panicked at 'x', it's, g.rs:3:4 and 'y', h.rs:5:6", ("x', it's, g.rs:3:4 and 'y", "h.rs", "5")),
])
def test_libtest_panic_old_matches_original_pattern(line, expected):
    old = re.compile(r"panicked at '(?P<msg>.*)', (?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+)").search(line)
    assert (old.group("msg"), old.group("file"), old.group("line")) == expected
    assert report_parsers._libtest_panic_old(line) == (expected[0], expected[1], int(expected[2]))


@pytest.mark.parametrize("line", ["", "panicked at 'x'", "panicked at 'x', f.rs:1", "panicked at x', f.rs:1:2"])
def test_libtest_panic_old_absent(line):
    assert report_parsers._libtest_panic_old(line) is None


def test_libtest_text_old_style_panic_still_parses():
    text = "---- t::a stdout ----" + chr(10) + "thread 'main' panicked at 'boom', src/lib.rs:10:5" + chr(10)
    parsed = report_parsers.parse_libtest_text(text)
    assert (parsed.failures[0].file, parsed.failures[0].line) == ("src/lib.rs", 10)


def test_tool_intent_mutation_request_then_run_is_linear():
    assert_linear(lambda n: "then " * (n // 5), tool_intent._MUTATION_REQUEST.search)
    assert_linear(lambda n: "and please " * (n // 11), tool_intent._MUTATION_REQUEST.search)
    assert_linear(lambda n: "list files " + "then " * (n // 5), tool_intent.classify_file_intent)


@pytest.mark.parametrize("text", [
    "write it", "please write it", "list it and then delete it", "ok, also please remove x.txt",
    "x; then also run y", "and please then fix z", "can you create a.txt",
])
def test_tool_intent_mutation_request_still_matches(text):
    assert tool_intent._MUTATION_REQUEST.search(text)


@pytest.mark.parametrize("text", ["I wrote it", "then then then", "and please", "describe then explain"])
def test_tool_intent_mutation_request_still_rejects(text):
    assert not tool_intent._MUTATION_REQUEST.search(text)


def test_tool_intent_long_inputs_stay_fast():
    for text in ("do not edit " + "x " * 25000, "list the files " + "a." * 25000, "a/b " * 12500 + " read the file"):
        for predicate in (tool_intent.suppresses_file_tools, tool_intent.classify_file_intent):
            assert _elapsed(functools.partial(predicate, text))[0] < LIMIT_SECONDS


def test_generic_location_run_is_linear():
    assert_linear(lambda n: "." * n, report_parsers._GENERIC_LOCATION.search)
    assert_linear(lambda n: "a." * (n // 2), report_parsers._GENERIC_LOCATION.search)
    assert_linear(lambda n: "a/" * (n // 2), report_parsers._GENERIC_LOCATION.search)


def test_generic_location_returns_full_long_path():
    path = "/" + "d/" * 200 + "file.rs"
    match = report_parsers._GENERIC_LOCATION.search("at " + path + ":12")
    assert (match.group("file"), match.group("line")) == (path, "12")


def test_generic_location_long_path_without_extension_gives_no_match():
    assert report_parsers._GENERIC_LOCATION.search("at /" + "d/" * 400 + "file:12") is None
