import os
import subprocess
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


def test_cpp_compile_unavailable_when_cl_is_missing(monkeypatch):
    # vcvars64.bat exists (the isfile guard passes) but no x64 toolset sits
    # behind it: cmd exits 9009 having printed only its own not-found message.
    # That is "could not judge", not "the artifact failed" — reporting it as a
    # Verdict(False) fails correct C++ and sends it to the repair loop.
    monkeypatch.setattr(V, "_run", lambda *a, **k: (
        9009,
        "'cl' is not recognized as an internal or external command,\n"
        "operable program or batch file.\n",
    ))
    with pytest.raises(V.VerifierUnavailable):
        V.cpp_compile("int main(){ return 0; }", {"vcvars": V.__file__})


def test_cpp_compile_still_reports_a_real_diagnostic_over_a_missing_tool(monkeypatch):
    # Guard the fix against over-blocking: when MSVC actually spoke, a stray
    # not-found line elsewhere in the log must not turn a real compile failure
    # into "could not judge".
    monkeypatch.setattr(V, "_run", lambda *a, **k: (
        2,
        "'vswhere' is not recognized as an internal or external command\n"
        "tu.cpp(1): error C2143: syntax error\n",
    ))
    v = V.cpp_compile("int main(", {"vcvars": V.__file__})
    assert v.passed is False
    assert "C2143" in v.reason


def _spy_on_mkdtemp(monkeypatch):
    made = []
    real = tempfile.mkdtemp

    def spy(*a, **k):
        path = real(*a, **k)
        made.append(path)
        return path

    monkeypatch.setattr(V.tempfile, "mkdtemp", spy)
    return made


def test_cpp_compile_removes_its_temp_dir_on_success(monkeypatch):
    made = _spy_on_mkdtemp(monkeypatch)
    monkeypatch.setattr(V, "_run", lambda *a, **k: (0, ""))
    assert V.cpp_compile("int main(){ return 0; }", {"vcvars": V.__file__}).passed is True
    assert made and not os.path.exists(made[0])


def test_cpp_compile_removes_its_temp_dir_when_the_build_is_killed(monkeypatch):
    # _run propagates TimeoutExpired; the tu.cpp/build.bat directory must not
    # outlive it. %TEMP% had accumulated 192 of these from the test suite alone.
    made = _spy_on_mkdtemp(monkeypatch)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("cmd", 180)

    monkeypatch.setattr(V, "_run", boom)
    with pytest.raises(subprocess.TimeoutExpired):
        V.cpp_compile("int main(){ return 0; }", {"vcvars": V.__file__})
    assert made and not os.path.exists(made[0])


# --- llm_judge — injected judge_fn, no GPU --------------------------------
def test_llm_judge_pass_and_fail():
    good = V.verify("llm_judge", "some answer",
                    {"judge_fn": lambda p: "9 - solid and complete", "threshold": 7})
    assert good.passed is True
    weak = V.verify("llm_judge", "meh",
                    {"judge_fn": lambda p: "3 - incomplete", "threshold": 7})
    assert weak.passed is False


def test_cpp_compile_rejects_std_injection():
    # a crafted /std must not smuggle a shell command into the .bat
    with pytest.raises(ValueError):
        V.cpp_compile("int main(){}", {"vcvars": V.__file__, "std": "c++17 & calc.exe"})


def test_cpp_compile_rejects_unsafe_vcvars():
    with pytest.raises(V.VerifierUnavailable):
        V.cpp_compile("int main(){}", {"vcvars": 'C:/x & del.bat'})


def test_pytest_run_rejects_write_to_traversal():
    d = tempfile.mkdtemp()
    with pytest.raises(ValueError):
        V.pytest_run("print('x')", {"cwd": d, "write_to": "../../evil.py"})


def test_pytest_run_rejects_option_select():
    d = tempfile.mkdtemp()
    with pytest.raises(ValueError):
        V.pytest_run("", {"cwd": d, "select": "-p evilplugin"})


def test_registry_covers_all_documented_backends():
    for name in ("python_exec", "program_run", "pytest_run", "typecheck",
                 "cpp_compile", "llm_judge"):
        assert name in V.REGISTRY


# --- promoted ext backends: the shared-exception contract ------------------
# The registration block claimed every promoted backend used verifiers' own
# VerifierUnavailable. ruff_verifier.py declared its OWN
# `class VerifierUnavailable(RuntimeError)` instead, a different type, so
# `except verifiers.VerifierUnavailable` around verifiers.verify("ruff_check", ...)
# did not catch a missing-ruff signal — it escaped as a bare RuntimeError and the
# caller could not tell "could not judge" from a crash. These pin the rule for
# every promoted backend, not just the one that broke it.
_PROMOTED_EXT_MODULES = ("node_verifier", "sql_verifier", "json_schema_verifier",
                         "ruff_verifier")


def test_ext_backends_reuse_the_shared_unavailable_class():
    """A promoted backend that declares a VerifierUnavailable must reuse THIS
    module's class. A same-named local subclass of RuntimeError is a distinct
    type that `except verifiers.VerifierUnavailable` misses (ruff_verifier had
    exactly that). Backends with no external tool (sql/json) declare none, which
    is fine — the rule is 'if you declare it, it is the shared one'."""
    for mod_name in _PROMOTED_EXT_MODULES:
        mod = __import__(mod_name)
        local = getattr(mod, "VerifierUnavailable", None)
        if local is not None:
            assert local is V.VerifierUnavailable, (
                "%s.VerifierUnavailable is a private class; "
                "except verifiers.VerifierUnavailable would not catch it" % mod_name)


def test_ruff_missing_binary_is_caught_by_shared_unavailable():
    """The concrete failure the private class caused: a caller guarding
    verifiers.verify() with the registry's own exception type. With a private
    class this raised out uncaught, so this exercises the real (unmonkeypatched)
    FileNotFoundError path through the registry seam."""
    assert "ruff_check" in V.REGISTRY
    with pytest.raises(V.VerifierUnavailable):
        V.verify("ruff_check", "x = 1\n",
                 {"ruff": "definitely-not-a-real-ruff-binary-zzz"})


def test_promoted_backends_register_regardless_of_import_order():
    """node_verifier does `from verifiers import ...` at module scope, so when it
    is imported FIRST it is still half-initialized while verifiers' registration
    loop runs; the loop's eager getattr then found no `node_run`, swallowed the
    AttributeError, and left "node_run" permanently missing from REGISTRY. Whether
    a backend exists must not depend on which module the process imported first,
    so this asserts the same key set from both orders in fresh interpreters."""
    import subprocess
    import sys as _sys

    root = os.path.dirname(os.path.abspath(V.__file__))

    def keys_for(first_import):
        code = ("import %s\nimport verifiers\n"
                "print(','.join(sorted(verifiers.REGISTRY)))" % first_import)
        p = subprocess.run([_sys.executable, "-c", code], cwd=root,
                           capture_output=True, timeout=120)
        assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
        return p.stdout.decode("utf-8", "replace").strip().splitlines()[-1]

    baseline = keys_for("verifiers")
    assert "node_run" in baseline and "ruff_check" in baseline
    for backend in ("node_verifier", "ruff_verifier"):
        assert keys_for(backend) == baseline, (
            "importing %s first changed the registry" % backend)
