"""run_code must not report a pass unless the appended checks actually ran.

The grading checks are appended after the model's code and the file runs as
``__main__``.  Code that exits 0 before reaching them (``sys.exit(0)`` in a
main guard, ``unittest.main()``, ``os._exit(0)``) used to be scored a
verified pass although no assertion executed.
"""
import grounding
import verifiers

WRONG_ADD = "def add(a, b):\n    return a - b\n"


def test_main_guard_exit_before_checks_is_a_failure():
    code = WRONG_ADD + 'if __name__ == "__main__":\n    import sys\n    sys.exit(0)\n'
    ok, output = grounding.run_code(code, "assert add(1, 2) == 3")
    assert ok is False
    assert "checks" in output


def test_hard_exit_before_checks_is_a_failure():
    code = WRONG_ADD + "import os\nos._exit(0)\n"
    ok, _ = grounding.run_code(code, "assert add(1, 2) == 3")
    assert ok is False


def test_candidate_cannot_forge_check_completion_from_its_source():
    code = WRONG_ADD + (
        "import os, pathlib, re, sys\n"
        "source = pathlib.Path(__file__).read_text(encoding='utf-8')\n"
        "token = re.search(r'__SONDER_CHECKS_DONE_[0-9a-f]{32}__', source)\n"
        "if token:\n"
        "    sys.__stdout__.write('\\n' + token.group(0) + '\\n')\n"
        "    sys.__stdout__.flush()\n"
        "os._exit(0)\n"
    )
    result = grounding.run_code_detail(code, extra="assert add(1, 2) == 3")
    assert result["ok"] is False


def test_python_exec_verifier_rejects_early_exit():
    code = WRONG_ADD + "raise SystemExit(0)\n"
    verdict = verifiers.python_exec(code, {"check": "assert add(1, 2) == 3"})
    assert verdict.passed is False


def test_correct_code_still_passes_and_sentinel_is_hidden():
    code = "def add(a, b):\n    return a + b\nprint('hello')\n"
    result = grounding.run_code_detail(code, extra="assert add(1, 2) == 3")
    assert result["ok"] is True
    assert result["stdout"] == "hello"


def test_redirected_stdout_does_not_hide_the_sentinel():
    code = "import io, sys\nsys.stdout = io.StringIO()\ndef add(a, b):\n    return a + b\n"
    ok, _ = grounding.run_code(code, "assert add(1, 2) == 3")
    assert ok is True


def test_code_without_checks_is_unchanged():
    ok, output = grounding.run_code("import sys\nprint('x')\nsys.exit(0)\n")
    assert ok is True
    assert output == "x"


def test_checks_see_module_globals_used_by_candidate_functions():
    code = "SCALE = 10\ndef scaled(x):\n    return x * SCALE\n"
    result = grounding.run_code_detail(code, extra="assert scaled(2) == 20")
    assert result["ok"] is True


def test_check_suite_longer_than_a_command_line_still_runs():
    checks = "\n".join("assert add(%d, 1) == %d" % (i, i + 1) for i in range(4000))
    assert len(checks) > 40_000
    code = "def add(a, b):\n    return a + b\n"
    assert grounding.run_code_detail(code, extra=checks)["ok"] is True
    wrong = "def add(a, b):\n    return a - b\n"
    assert grounding.run_code_detail(wrong, extra=checks)["ok"] is False
