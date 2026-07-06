import os
import tempfile

import pytest

import verifiers as V


def test_get_unknown_raises():
    with pytest.raises(KeyError):
        V.get("does_not_exist")


# --- python_exec (real subprocess) ----------------------------------------
def test_python_exec_pass_and_fail():
    ok = V.verify("python_exec", "def f():\n    return 1", {"check": "assert f() == 1"})
    assert ok.passed is True
    bad = V.verify("python_exec", "def f():\n    return 0", {"check": "assert f() == 1"})
    assert bad.passed is False
    assert "Traceback" in bad.detail or "AssertionError" in bad.detail


# --- program_run (real headless run) --------------------------------------
def test_program_run_clean_passes_and_crash_fails():
    assert V.verify("program_run", "print('hi')", {"kind": "console"}).passed is True
    crash = V.verify("program_run", "undefined_name_zzz", {"kind": "console"})
    assert crash.passed is False


# --- pytest_run (real pytest in a temp dir) -------------------------------
def test_pytest_run_pass():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "test_ok.py"), "w") as f:
        f.write("def test_a():\n    assert 1 + 1 == 2\n")
    v = V.pytest_run("", {"cwd": d})
    assert v.passed is True


def test_pytest_run_fail():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "test_bad.py"), "w") as f:
        f.write("def test_b():\n    assert False\n")
    v = V.pytest_run("", {"cwd": d})
    assert v.passed is False


# --- typecheck (mypy) — deterministic via monkeypatched _run --------------
def test_typecheck_unavailable_when_mypy_missing(monkeypatch):
    monkeypatch.setattr(V, "_run", lambda *a, **k: (1, "No module named mypy"))
    with pytest.raises(V.VerifierUnavailable):
        V.typecheck("x = 1")


def test_typecheck_pass_when_clean(monkeypatch):
    monkeypatch.setattr(V, "_run", lambda *a, **k: (0, ""))
    assert V.typecheck("x: int = 1").passed is True


# --- cpp_compile — deterministic without needing a real compiler ----------
def test_cpp_compile_unavailable_without_vcvars():
    with pytest.raises(V.VerifierUnavailable):
        V.cpp_compile("int main(){}", {"vcvars": "Z:/nope/vcvars64.bat"})


def test_cpp_compile_pass(monkeypatch):
    # point vcvars at any existing file, stub the compile invocation as success
    monkeypatch.setattr(V, "_run", lambda *a, **k: (0, ""))
    v = V.cpp_compile("int main(){ return 0; }", {"vcvars": V.__file__})
    assert v.passed is True
    assert v.reason == "compiled"


def test_cpp_compile_reports_errors(monkeypatch):
    monkeypatch.setattr(V, "_run", lambda *a, **k: (2, "tu.cpp(1): error C2143: syntax error"))
    v = V.cpp_compile("int main(", {"vcvars": V.__file__})
    assert v.passed is False
    assert "C2143" in v.detail


# --- llm_judge — injected judge_fn, no GPU --------------------------------
def test_llm_judge_pass_and_fail():
    good = V.verify("llm_judge", "some answer",
                    {"judge_fn": lambda p: "9 - solid and complete", "threshold": 7})
    assert good.passed is True
    weak = V.verify("llm_judge", "meh",
                    {"judge_fn": lambda p: "3 - incomplete", "threshold": 7})
    assert weak.passed is False


def test_registry_covers_all_documented_backends():
    for name in ("python_exec", "program_run", "pytest_run", "typecheck",
                 "cpp_compile", "llm_judge"):
        assert name in V.REGISTRY
