"""Non-Python graders must not pass unless the appended checks ran to the end.

``run_language_code`` appends the grader's checks after the candidate code.
Code that terminates the process with success first (``process.exit(0)``,
PowerShell ``exit 0``, ``std::exit(0)``, ``Environment.Exit(0)``) used to be
scored a pass although no check executed.  Sibling of the Python fix in
``test_grounding_check_sentinel``: a per-run random sentinel is emitted only
after the checks finish and is stripped from the reported output.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

import grounding


def _require(*names):
    if not any(shutil.which(name) for name in names):
        pytest.skip("missing %s" % "/".join(names))


def _dotnet_ok():
    if shutil.which("csc"):
        return True
    dotnet = shutil.which("dotnet")
    if not dotnet:
        return False
    try:
        sdks = subprocess.run([dotnet, "--list-sdks"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split(".")[0].isdigit() and int(line.split(".")[0]) >= 8 for line in sdks.splitlines())


# -- JavaScript -------------------------------------------------------------

JS_CHECK = "if (add(1, 2) !== 3) { throw new Error('add is wrong'); }"


def test_js_exit_before_checks_is_a_failure():
    _require("node")
    code = "function add(a, b) { return a - b; }\nprocess.exit(0);\n"
    ok, out = grounding.run_language_code(code, "javascript", extra=JS_CHECK, timeout=30)
    assert ok is False
    assert "checks" in out


def test_js_correct_code_passes_and_sentinel_is_hidden():
    _require("node")
    code = "function add(a, b) { return a + b; }\nconsole.log('hello');\n"
    ok, out = grounding.run_language_code(code, "javascript", extra=JS_CHECK, timeout=30)
    assert ok is True, out
    assert out == "hello"


# -- PowerShell -------------------------------------------------------------

PS_CHECK = "if ((Add 1 2) -ne 3) { throw 'add is wrong' }"


def test_powershell_exit_before_checks_is_a_failure():
    _require("pwsh", "powershell")
    code = "function Add($a, $b) { $a - $b }\nexit 0\n"
    ok, out = grounding.run_language_code(code, "powershell", extra=PS_CHECK, timeout=60)
    assert ok is False
    assert "checks" in out


def test_powershell_correct_code_passes_and_sentinel_is_hidden():
    _require("pwsh", "powershell")
    code = "function Add($a, $b) { $a + $b }\nWrite-Output 'hello'\n"
    ok, out = grounding.run_language_code(code, "powershell", extra=PS_CHECK, timeout=60)
    assert ok is True, out
    assert out == "hello"


# -- C++ --------------------------------------------------------------------

CPP_CHECK = "#include <cassert>\nint main() {\n    assert(add(1, 2) == 3);\n}\n"


def test_cpp_exit_before_checks_is_a_failure():
    _require("g++", "clang++", "cl")
    code = "#include <cstdlib>\nint add(int a, int b) { std::exit(0); return a - b; }\n"
    ok, out = grounding.run_language_code(code, "cpp", extra=CPP_CHECK, timeout=90)
    assert ok is False
    assert "checks" in out


def test_cpp_correct_code_passes_without_explicit_return():
    _require("g++", "clang++", "cl")
    code = '#include <cstdio>\nint add(int a, int b) { std::puts("hello"); return a + b; }\n'
    ok, out = grounding.run_language_code(code, "cpp", extra=CPP_CHECK, timeout=90)
    assert ok is True, out
    assert out == "hello"


def test_cpp_check_exit_code_is_preserved():
    _require("g++", "clang++", "cl")
    code = "int add(int a, int b) { return a + b; }\n"
    check = "int main(int argc, char** argv) {\n    (void)argv;\n    return add(1, 2) == 3 ? 7 : 0;\n}\n"
    ok, _ = grounding.run_language_code(code, "cpp", extra=check, timeout=90)
    assert ok is False


# -- C# ---------------------------------------------------------------------

CS_CHECK = (
    "public static class Checks {\n"
    "    public static void Main() {\n"
    "        if (Calc.Add(1, 2) != 3) throw new System.Exception(\"add is wrong\");\n"
    "    }\n"
    "}\n"
)


def test_csharp_exit_before_checks_is_a_failure():
    if not _dotnet_ok():
        pytest.skip("no csc or .NET 8 SDK")
    code = (
        "public static class Calc {\n"
        "    public static int Add(int a, int b) { System.Environment.Exit(0); return a - b; }\n"
        "}\n"
    )
    ok, out = grounding.run_language_code(code, "csharp", extra=CS_CHECK, timeout=120)
    assert ok is False
    assert "checks" in out


def test_csharp_correct_code_passes():
    if not _dotnet_ok():
        pytest.skip("no csc or .NET 8 SDK")
    code = (
        "public static class Calc {\n"
        "    public static int Add(int a, int b) { System.Console.WriteLine(\"hello\"); return a + b; }\n"
        "}\n"
    )
    ok, out = grounding.run_language_code(code, "csharp", extra=CS_CHECK, timeout=120)
    assert ok is True, out
    assert "hello" in out and "SONDER" not in out
