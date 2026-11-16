"""Adversarial-input timing and equivalence checks for polynomial-regex fixes."""
import re
import time

import pytest

from sonder_runtime.application.chat import tool_intent
from sonder_runtime.domain import batch_jobs, fanout_redaction
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.memory import write_quality
from sonder_runtime.domain.meshgen import prompts as meshgen_prompts
from sonder_runtime.domain.testing import report_parsers

BUDGET_SECONDS = 0.75
BACKSLASH = chr(92)


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


def test_tool_intent_overlong_text_never_suppresses():
    assert tool_intent.suppresses_file_tools("do not use tools on the file " + "x" * 13000) is False


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
