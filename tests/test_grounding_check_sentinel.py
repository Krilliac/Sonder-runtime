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
