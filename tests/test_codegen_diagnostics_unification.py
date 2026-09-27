"""codegen_loop's error counter is the domain diagnostics parser, byte for byte."""
from __future__ import annotations

import re

import codegen_loop
from sonder_runtime.domain.diagnostics import parsers


def _historical_count_errors(output, error_regex=r"(?i)\b(?:error|fatal)\b"):
    """The implementation codegen_loop carried before the unification."""
    pattern = re.compile(error_regex)
    seen, out = set(), []
    for line in output.split("\n"):
        line = line.strip()
        if not line or not pattern.search(line):
            continue
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


CORPUS = [
    "",
    "\n\n",
    "Program.cs(3,5): error CS0103: The name 'x' does not exist\n" * 3,
    "a.cs(1,1): error CS1002: ; expected\nb.cs(2,2): error CS0111: duplicate member\n",
    "error: build could not run: dotnet not found\n",
    "fatal error C1083: cannot open\n  Fatal: stop\nnot an ERR0R line\n",
    "warning: x\r\nerror: y\r\n   error: y   \r\n",
    "too many errors emitted, stopping now [-ferror-limit=]\nerror: z\n",
    "output was truncated -- error summary may be missing\nerror: q\n",
    "\x00error\x00\nterror\nerror_handler.py ok\n",
]


def test_default_pattern_is_the_domain_constant():
    assert codegen_loop.DEFAULT_ERROR_RE is parsers.DEFAULT_ERROR_LINE_PATTERN
    assert codegen_loop.DEFAULT_ERROR_RE == r"(?i)\b(?:error|fatal)\b"


def test_count_errors_equals_error_lines_over_the_corpus():
    for text in CORPUS:
        expected = _historical_count_errors(text)
        assert codegen_loop.count_errors(text) == expected
        assert parsers.error_lines(text) == expected


def test_custom_regexes_including_masking_and_truncation_patterns():
    for pattern in (
        codegen_loop.DEFAULT_TRUNCATED_ERROR_RE,
        codegen_loop.DEFAULT_PARTIAL_OUTPUT_ERROR_RE,
        r"CS\d{4}",
    ):
        for text in CORPUS:
            assert codegen_loop.count_errors(text, pattern) == _historical_count_errors(text, pattern)


def test_downstream_codegen_classifiers_still_see_the_same_lines():
    text = CORPUS[3] + CORPUS[7]
    errors = codegen_loop.count_errors(text)
    assert codegen_loop.parse_blocked(errors) == codegen_loop.parse_blocked(
        _historical_count_errors(text)
    )


def test_codegen_loop_has_no_second_error_counter():
    """One diagnostics parser: the root module delegates rather than re-implements."""
    import inspect

    source = inspect.getsource(codegen_loop.count_errors)
    assert "_error_lines(" in source
    assert "re.compile" not in source
