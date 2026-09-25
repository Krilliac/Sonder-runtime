"""Golden fixtures for the one diagnostics parser (domain, pure)."""
from __future__ import annotations

import time

import pytest

from sonder_runtime.domain.diagnostics.model import Diagnostic, message_template
from sonder_runtime.domain.diagnostics.parsers import (
    DEFAULT_ERROR_LINE_PATTERN,
    error_lines,
    parse_diagnostics,
    parse_line,
)


def _one(text, **kwargs):
    found = parse_diagnostics(text, **kwargs).diagnostics
    assert len(found) == 1, found
    return found[0]


def _shape(d: Diagnostic):
    return (d.tool, d.severity, d.file, d.line, d.col, d.code, d.message)


@pytest.mark.parametrize("line, expected", [
    ("t.c:3:5: error: 'y' undeclared (first use in this function)",
     ("gnu", "error", "t.c", 3, 5, "", "'y' undeclared (first use in this function)")),
    ("t.c:2:9: warning: unused variable 'x' [-Wunused-variable]",
     ("gnu", "warning", "t.c", 2, 9, "-Wunused-variable", "unused variable 'x'")),
    ("main.c:1:10: fatal error: missing.h: No such file or directory",
     ("gnu", "fatal", "main.c", 1, 10, "", "missing.h: No such file or directory")),
    ("src/x.cpp:12: warning: comparison is always true",
     ("gnu", "warning", "src/x.cpp", 12, None, "", "comparison is always true")),
    ("t.c:3:5: note: each undeclared identifier is reported only once",
     ("gnu", "note", "t.c", 3, 5, "", "each undeclared identifier is reported only once")),
    ("C:\\proj\\src\\a.c:7:1: error: expected ';' before '}' token",
     ("gnu", "error", "C:/proj/src/a.c", 7, 1, "", "expected ';' before '}' token")),
    ("t.c:(.text+0x9): undefined reference to `foo'",
     ("gnu", "error", "t.c", None, None, "", "undefined reference to `foo'")),
    ("/usr/bin/ld: t.c:(.text+0x9): undefined reference to `foo'",
     ("gnu", "error", "t.c", None, None, "", "undefined reference to `foo'")),
    ("collect2: error: ld returned 1 exit status",
     ("gnu", "error", "", None, None, "", "ld returned 1 exit status")),
])
def test_gnu_gcc_clang_and_ld(line, expected):
    assert _shape(_one(line)) == expected


@pytest.mark.parametrize("line, expected", [
    ("C:\\src\\a.cpp(12,5): error C2065: 'foo': undeclared identifier [C:\\src\\a.vcxproj]",
     ("msvc", "error", "C:/src/a.cpp", 12, 5, "C2065", "'foo': undeclared identifier")),
    ("a.cpp(3): warning C4996: 'strcpy': This function may be unsafe.",
     ("msvc", "warning", "a.cpp", 3, None, "C4996", "'strcpy': This function may be unsafe.")),
    ("a.cpp(9): fatal error C1083: Cannot open include file: 'x.h'",
     ("msvc", "fatal", "a.cpp", 9, None, "C1083", "Cannot open include file: 'x.h'")),
    ("main.obj : error LNK2019: unresolved external symbol foo referenced in function main",
     ("msvc_link", "error", "main.obj", None, None, "LNK2019",
      "unresolved external symbol foo referenced in function main")),
    ("LINK : fatal error LNK1104: cannot open file 'x.lib'",
     ("msvc_link", "fatal", "LINK", None, None, "LNK1104", "cannot open file 'x.lib'")),
    ("Program.cs(3,5): error CS0103: The name 'x' does not exist in the current context [C:\\a\\app.csproj]",
     ("dotnet", "error", "Program.cs", 3, 5, "CS0103",
      "The name 'x' does not exist in the current context")),
    ("MSBUILD : error MSB1009: Project file does not exist.",
     ("dotnet", "error", "MSBUILD", None, None, "MSB1009", "Project file does not exist.")),
    ("C:\\sdk\\Targets.targets(166,5): error NETSDK1045: The current .NET SDK does not support targeting .NET 9.0. [C:\\a\\app.csproj]",
     ("dotnet", "error", "C:/sdk/Targets.targets", 166, 5, "NETSDK1045",
      "The current .NET SDK does not support targeting .NET 9.0.")),
])
def test_msvc_link_and_dotnet(line, expected):
    assert _shape(_one(line)) == expected


def test_rustc_takes_location_from_arrow_line_and_drops_summaries():
    text = (
        "error[E0308]: mismatched types\n"
        " --> src/main.rs:4:18\n"
        "  |\n"
        "4 |     let x: i32 = \"a\";\n"
        "warning: unused variable: `y`\n"
        "  --> src/lib.rs:2:9\n"
        "warning: `demo` (bin \"demo\") generated 1 warning\n"
        "error: aborting due to 1 previous error\n"
        "error: could not compile `demo` (bin \"demo\") due to 1 previous error\n"
    )
    found = parse_diagnostics(text).diagnostics
    assert [_shape(d) for d in found] == [
        ("rustc", "error", "src/main.rs", 4, 18, "E0308", "mismatched types"),
        ("rustc", "warning", "src/lib.rs", 2, 9, "", "unused variable: `y`"),
        ("rustc", "error", "", None, None, "",
         "could not compile `demo` (bin \"demo\") due to 1 previous error"),
    ]


@pytest.mark.parametrize("line, expected", [
    ("src/a.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'.",
     ("tsc", "error", "src/a.ts", 3, 7, "TS2322",
      "Type 'string' is not assignable to type 'number'.")),
    ("src/b.ts:10:1 - error TS2304: Cannot find name 'foo'.",
     ("tsc", "error", "src/b.ts", 10, 1, "TS2304", "Cannot find name 'foo'.")),
    ("\x1b[96msrc/b.ts\x1b[0m:10:1 - \x1b[91merror\x1b[0m TS2304: Cannot find name 'foo'.",
     ("tsc", "error", "src/b.ts", 10, 1, "TS2304", "Cannot find name 'foo'.")),
    ("/w/app.js:3:7: 'x' is assigned a value but never used. [Error/no-unused-vars]",
     ("eslint", "error", "/w/app.js", 3, 7, "no-unused-vars",
      "'x' is assigned a value but never used.")),
])
def test_tsc_and_eslint_unix(line, expected):
    assert _shape(_one(line)) == expected


def test_eslint_stylish_block_uses_the_header_path():
    text = (
        "/w/src/app.js\n"
        "  3:7   error    'x' is assigned a value but never used  no-unused-vars\n"
        "  9:1   warning  Unexpected console statement            no-console\n"
        "\n"
        "/w/src/util.ts\n"
        "  1:10  error  Missing semicolon  @typescript-eslint/semi\n"
        "\n"
        "\u2716 3 problems (2 errors, 1 warning)\n"
    )
    found = parse_diagnostics(text).diagnostics
    assert [_shape(d) for d in found] == [
        ("eslint", "error", "/w/src/app.js", 3, 7, "no-unused-vars",
         "'x' is assigned a value but never used"),
        ("eslint", "warning", "/w/src/app.js", 9, 1, "no-console",
         "Unexpected console statement"),
        ("eslint", "error", "/w/src/util.ts", 1, 10, "@typescript-eslint/semi",
         "Missing semicolon"),
    ]


def test_go_build_and_vet_ignore_package_headers():
    text = (
        "# example.com/demo\n"
        "./main.go:5:2: undefined: foo\n"
        "# example.com/demo\n"
        "# [example.com/demo]\n"
        "./main.go:8:2: fmt.Printf format %d has arg \"x\" of wrong type string\n"
    )
    found = parse_diagnostics(text).diagnostics
    assert [_shape(d) for d in found] == [
        ("go", "error", "main.go", 5, 2, "", "undefined: foo"),
        ("go", "error", "main.go", 8, 2, "", "fmt.Printf format %d has arg \"x\" of wrong type string"),
    ]


def test_python_traceback_last_frame_wins():
    text = (
        "Traceback (most recent call last):\n"
        '  File "/w/app.py", line 12, in <module>\n'
        "    main()\n"
        '  File "/w/lib/core.py", line 40, in main\n'
        "    return data['key']\n"
        "KeyError: 'key'\n"
    )
    assert _shape(_one(text)) == (
        "python", "error", "/w/lib/core.py", 40, None, "KeyError", "'key'",
    )


def test_python_syntax_error_with_and_without_traceback_header():
    wrapped = (
        "Traceback (most recent call last):\n"
        '  File "/w/run.py", line 1, in <module>\n'
        "    import bad\n"
        '  File "/w/bad.py", line 3\n'
        "    def f(:\n"
        "          ^\n"
        "SyntaxError: invalid syntax\n"
    )
    bare = (
        '  File "/w/bad.py", line 3\n'
        "    def f(:\n"
        "          ^\n"
        "SyntaxError: invalid syntax\n"
    )
    for text in (wrapped, bare):
        assert _shape(_one(text)) == (
            "python", "error", "/w/bad.py", 3, None, "SyntaxError", "invalid syntax",
        )


def test_pytest_failed_and_error_lines():
    text = (
        "FAILED tests/test_mod.py::test_bad - assert 1 == 2\n"
        "ERROR tests/test_broken.py - ImportError: no module named x\n"
        "FAILED tests/test_mod.py::TestK::test_p[1-2]\n"
    )
    assert [_shape(d) for d in parse_diagnostics(text).diagnostics] == [
        ("pytest", "error", "tests/test_mod.py", None, None, "FAILED", "assert 1 == 2"),
        ("pytest", "error", "tests/test_broken.py", None, None, "ERROR",
         "ImportError: no module named x"),
        ("pytest", "error", "tests/test_mod.py", None, None, "FAILED",
         "tests/test_mod.py::TestK::test_p[1-2]"),
    ]


def test_parse_line_matches_parse_diagnostics_for_single_line_shapes():
    line = "t.c:3:5: error: 'y' undeclared"
    assert parse_line(line) == _one(line).__class__(**{
        **{name: getattr(_one(line), name) for name in Diagnostic.__slots__},
        "raw_line_no": 0,
    })
    assert parse_line("plain prose") is None


def test_tools_filter_restricts_grammars():
    text = "t.c:3:5: error: bad\n./m.go:1:1: undefined: x\n"
    only_go = parse_diagnostics(text, tools=["go"]).diagnostics
    assert [d.tool for d in only_go] == ["go"]
    assert parse_diagnostics(text, tools=[]).diagnostics == ()


def test_dedupe_and_signature_groups_same_template():
    text = "\n".join([
        "a.c:1:1: error: 'alpha' undeclared",
        "a.c:1:1: error: 'alpha' undeclared",  # exact duplicate: dropped
        "b.c:9:3: error: 'beta' undeclared",
        "c.c:44:2: error: 'gamma' undeclared",
        "c.c:50:1: warning: unused variable 'z' [-Wunused-variable]",
    ])
    result = parse_diagnostics(text)
    assert len(result.diagnostics) == 4
    assert dict(result.counts) == {"error": 3, "warning": 1}
    first = result.groups[0]
    assert first.count == 3 and first.severity == "error"
    assert first.files == ("a.c", "b.c", "c.c")
    assert first.template == "<q> undeclared"
    assert result.groups[1].severity == "warning"
    signatures = {d.signature() for d in result.diagnostics if d.severity == "error"}
    assert len(signatures) == 1


def test_signature_distinguishes_codes_and_tools():
    a = parse_line("a.cpp(1): error C2065: 'x': undeclared identifier")
    b = parse_line("a.cpp(1): error C2066: 'x': undeclared identifier")
    assert a.signature() != b.signature()
    assert message_template("expected 3 got 0x1F at /a/b/c.py") == "expected <n> got <hex> at <path>"


def test_caps_and_truncation_keep_counts_honest():
    text = "\n".join("f%d.c:%d:1: error: problem number %d" % (i, i + 1, i) for i in range(500))
    result = parse_diagnostics(text, max_diagnostics=50)
    assert len(result.diagnostics) == 50
    assert result.truncated is True
    assert dict(result.counts)["error"] == 500
    assert len(result.groups) == 1 and result.groups[0].count == 500
    assert len(result.groups[0].files) == 8

    lines = parse_diagnostics("x.c:1:1: error: e\n" * 10, max_lines=3)
    assert lines.truncated is True


def test_message_and_file_fields_are_bounded():
    long_name = "d/" * 400 + "f.c"
    diagnostic = _one("%s:1:1: error: %s" % (long_name, "m" * 2000))
    assert len(diagnostic.file) <= 260
    assert len(diagnostic.message) <= 400
    assert "\n" not in diagnostic.message


@pytest.mark.parametrize("line", [
    "a" * 4096,
    "x:" * 2048,
    "(" * 4096,
    "  1:1 error " + "a  " * 1360,
    "t.c:1:1: error: " + " [-W" * 1000,
    "FAILED " + "a" * 4089,
    "error: " + ": " * 2040,
    "Tests  " + "1 passed | " * 400,
    "./" * 2048,
    "a.cpp(" + "1," * 2040,
    "LNK" + " : " * 1360,
    "\"" * 4096,
])
def test_pathological_lines_finish_quickly(line):
    text = "\n".join([line] * 5)
    started = time.monotonic()
    parse_diagnostics(text)
    assert time.monotonic() - started < 1.0


NEGATIVE_CORPUS = """\
Starting the service on port 8080
0 errors, 0 warnings
Compiled error_handler.py successfully
Loaded module error_reporting from /opt/app/error_reporting.py
An error occurred earlier but was retried successfully
user notes: remember the fatal flaw in the plan
INFO request finished in 12ms
error_handler.py:12: handled a request
The build had no problems.
warning signs were discussed in the meeting
Tests: all good
Summary of the error budget: 99.9%
"""


def test_negative_control_corpus_yields_no_diagnostics():
    result = parse_diagnostics(NEGATIVE_CORPUS)
    assert result.diagnostics == (), result.diagnostics


def test_error_lines_matches_the_historical_codegen_semantics():
    text = "  error: a  \n\nwarning: b\nERROR: a\n  error: a\nfatal: c\n"
    assert error_lines(text) == ["error: a", "ERROR: a", "fatal: c"]
    assert DEFAULT_ERROR_LINE_PATTERN == r"(?i)\b(?:error|fatal)\b"
