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


def test_cpp_compile_discovers_vcvars_when_not_explicit(monkeypatch):
    monkeypatch.setattr(
        V.code_runner, "_find_visual_studio_vcvars", lambda: V.__file__
    )
    monkeypatch.setattr(V, "_run", lambda *a, **kw: (0, "compiled"))

    assert V.cpp_compile("int main(){ return 0; }").passed is True


def test_cpp_compile_reports_unavailable_when_discovery_finds_nothing(monkeypatch):
    monkeypatch.setattr(V.code_runner, "_find_visual_studio_vcvars", lambda: None)

    with pytest.raises(V.VerifierUnavailable, match="not discovered"):
        V.cpp_compile("int main(){ return 0; }")


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
                 "cpp_compile", "lean_check", "llm_judge"):
        assert name in V.REGISTRY


# --- lean_check — formal proof checking, deterministic without Lean -------
def test_lean_check_passes_kernel_checked_source(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((tuple(command), kwargs))
        if "--version" in command:
            return 0, "Lean (version 4.19.0, x86_64-unknown-linux-gnu)"
        assert open(command[-1], encoding="utf-8").read() == (
            "theorem and_comm (p q : Prop) : p ∧ q → q ∧ p := by\n"
            "  intro h\n  exact ⟨h.right, h.left⟩\n"
        )
        return 0, ""

    monkeypatch.setattr(V, "_run", fake_run)
    verdict = V.lean_check(
        "theorem and_comm (p q : Prop) : p ∧ q → q ∧ p := by\n"
        "  intro h\n  exact ⟨h.right, h.left⟩\n",
        {"lean": V.sys.executable},
    )

    assert verdict == V.Verdict(True, "checked", "")
    assert len(calls) == 2
    assert not os.path.exists(calls[-1][0][-1])


def test_lean_check_binds_success_to_the_requested_declaration(monkeypatch):
    checked_sources = []

    def fake_run(command, **kwargs):
        if "--version" in command:
            return 0, "Lean (version 4.19.0)"
        if "--run" in command:
            return 1, (
                "Sonder rejected theorem contract type mismatch: "
                "missing declaration requested"
            )
        source = open(command[-1], encoding="utf-8").read()
        checked_sources.append(source)
        return 0, ""

    monkeypatch.setattr(V, "_run", fake_run)
    verdict = V.lean_check(
        "example : True := by trivial\n",
        {
            "lean": V.sys.executable,
            "expected_declaration": "requested",
            "expected_type": "False",
        },
    )

    assert verdict.passed is False
    assert verdict.reason == "theorem contract mismatch"
    assert "example : True := by trivial\n" in checked_sources
    assert any("axiom contract : (False)" in source for source in checked_sources)


def test_lean_check_accepts_a_matching_requested_declaration(monkeypatch):
    sources = []

    def fake_run(command, **kwargs):
        if "--version" in command:
            return 0, "Lean (version 4.19.0)"
        if "--run" in command:
            return 0, ""
        sources.append(open(command[-1], encoding="utf-8").read())
        return 0, ""

    monkeypatch.setattr(V, "_run", fake_run)
    verdict = V.lean_check(
        "theorem requested : True := by trivial\n",
        {
            "lean": V.sys.executable,
            "expected_declaration": "requested",
            "expected_type": "True",
        },
    )

    assert verdict.passed is True
    assert "theorem requested : True := by trivial\n" in sources
    assert any("axiom contract : (True)" in source for source in sources)


def test_lean_check_rejects_metaprogrammed_axiom_dependencies(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((tuple(command), kwargs))
        if "--version" in command:
            return 0, "Lean (version 4.19.0)"
        if "--run" in command:
            return 1, "Sonder rejected unproved axiom dependencies: [falseProof]"
        return 0, ""

    monkeypatch.setattr(V, "_run", fake_run)
    source = """import Lean
run_cmd
  Lean.Elab.Command.liftCoreM <| Lean.addDecl (.axiomDecl {
    name := `falseProof
    levelParams := []
    type := .const ``False []
    isUnsafe := false
  })
theorem requested : False := falseProof
"""

    verdict = V.lean_check(
        source,
        {
            "lean": V.sys.executable,
            "expected_declaration": "requested",
            "expected_type": "False",
        },
    )

    assert verdict.passed is False
    assert verdict.reason == "unproved axiom dependency"
    assert "falseProof" in verdict.detail
    compile_call = calls[1]
    assert "-o" in compile_call[0]
    expected_call = calls[2]
    assert "SonderExpectedContract_" in expected_call[0][-1]
    audit_call = calls[3]
    assert "--run" in audit_call[0]
    assert audit_call[1]["env_overrides"]["LEAN_PATH"]
    assert "name == expectedDeclaration" in V._LEAN_AXIOM_AUDITOR_SOURCE


def test_lean_check_verifies_contract_outside_the_submitted_module(monkeypatch):
    checked_sources = {}

    def fake_run(command, **kwargs):
        if "--version" in command:
            return 0, "Lean (version 4.19.0)"
        if "--run" in command:
            return 1, (
                "Sonder rejected theorem contract type mismatch: "
                "requested has type True, expected False"
            )
        source_path = command[-1]
        checked_sources[os.path.basename(source_path)] = open(
            source_path, encoding="utf-8",
        ).read()
        return 0, ""

    monkeypatch.setattr(V, "_run", fake_run)
    source = """import Lean
syntax (priority := high) "example" ":" "(" term ")" ":=" term : command
macro_rules
  | `(example : ($expected) := $declaration) =>
      `(def swallowedContractWitness : True := by trivial)
theorem requested : True := by trivial
"""

    verdict = V.lean_check(
        source,
        {
            "lean": V.sys.executable,
            "expected_declaration": "requested",
            "expected_type": "False",
        },
    )

    assert verdict.passed is False
    assert verdict.reason == "theorem contract mismatch"
    assert checked_sources["Main.lean"] == source
    contract_sources = [
        text for name, text in checked_sources.items()
        if name.startswith("SonderExpectedContract_")
    ]
    assert len(contract_sources) == 1
    assert "axiom contract : (False)" in contract_sources[0]
    assert "macro_rules" not in contract_sources[0]


def test_lean_check_supports_caller_owned_contract_prelude(monkeypatch):
    checked_sources = {}

    def fake_run(command, **kwargs):
        if "--version" in command:
            return 0, "Lean (version 4.19.0)"
        if "--run" in command:
            return 0, ""
        source_path = command[-1]
        checked_sources[os.path.basename(source_path)] = open(
            source_path, encoding="utf-8",
        ).read()
        return 0, ""

    monkeypatch.setattr(V, "_run", fake_run)
    artifact = "theorem requested : IsZero 0 := rfl\n"
    prelude = "def IsZero (n : Nat) : Prop := n = 0\n"

    verdict = V.lean_check(
        artifact,
        {
            "lean": V.sys.executable,
            "expected_declaration": "requested",
            "expected_type": "IsZero 0",
            "trusted_prelude": prelude,
        },
    )

    assert verdict.passed is True
    prelude_names = [
        name for name in checked_sources
        if name.startswith("SonderTrustedPrelude_")
    ]
    contract_names = [
        name for name in checked_sources
        if name.startswith("SonderExpectedContract_")
    ]
    assert len(prelude_names) == len(contract_names) == 1
    prelude_module = os.path.splitext(prelude_names[0])[0]
    expected_module = os.path.splitext(contract_names[0])[0]
    assert checked_sources[prelude_names[0]] == prelude
    assert checked_sources[contract_names[0]].startswith(
        "import %s\n\n" % prelude_module
    )
    assert "axiom contract : (IsZero 0)" in checked_sources[contract_names[0]]
    assert checked_sources["Main.lean"] == (
        "import %s\n\n%s" % (prelude_module, artifact)
    )
    assert expected_module not in checked_sources["Main.lean"]


def test_lean_check_requires_the_contract_fields_as_a_pair():
    with pytest.raises(ValueError, match="supplied together"):
        V.lean_check(
            "theorem truth : True := by trivial",
            {"lean": V.sys.executable, "expected_declaration": "truth"},
        )

    with pytest.raises(ValueError, match="trusted_prelude requires"):
        V.lean_check(
            "theorem truth : True := by trivial",
            {"lean": V.sys.executable, "trusted_prelude": "def helper := 1"},
        )


@pytest.mark.parametrize("prelude", [None, "", " \n", 7])
def test_lean_check_rejects_invalid_trusted_prelude(prelude):
    with pytest.raises(ValueError, match="trusted_prelude"):
        V.lean_check(
            "theorem truth : True := by trivial",
            {
                "lean": V.sys.executable,
                "expected_declaration": "truth",
                "expected_type": "True",
                "trusted_prelude": prelude,
            },
        )


@pytest.mark.parametrize("placeholder", ["sorry", "admit", "axiom", "sorryAx"])
def test_lean_check_rejects_unproved_trust_gaps_without_running_tool(placeholder):
    verdict = V.lean_check(
        "theorem impossible : False := by exact %s" % placeholder,
        {"lean": "definitely-not-a-real-lean-binary"},
    )

    assert verdict.passed is False
    assert "trust gap" in verdict.reason
    assert placeholder.casefold() in verdict.detail.casefold()


def test_lean_check_rejects_constant_declarations_without_running_tool():
    source = (
        "constant falseProof : False\n"
        "theorem impossible : False := falseProof\n"
    )

    verdict = V.lean_check(
        source,
        {"lean": "definitely-not-a-real-lean-binary"},
    )

    assert verdict.passed is False
    assert verdict.reason == "unproved trust gap"
    assert "constant" in verdict.detail.casefold()


def test_lean_check_ignores_placeholder_words_in_comments_and_strings(monkeypatch):
    responses = iter(((0, "Lean (version 4.19.0)"), (0, "")))
    monkeypatch.setattr(V, "_run", lambda *args, **kwargs: next(responses))
    source = (
        '-- "sorry" and constant declarations are forbidden in real proof terms\n'
        '/- nested /- axiom -/ admit constant -/\n'
        'def message := "sorry admit axiom sorryAx constant"\n'
        'theorem truth : True := by trivial\n'
    )
    assert V.lean_check(source, {"lean": V.sys.executable}).passed is True


def test_lean_check_reports_missing_or_wrong_tool_as_unavailable(monkeypatch):
    with pytest.raises(V.VerifierUnavailable, match="not discovered"):
        V.lean_check("theorem truth : True := by trivial", {
            "lean": "definitely-not-a-real-lean-binary-zzz",
        })

    monkeypatch.setattr(V, "_run", lambda *args, **kwargs: (0, "Python 3.12"))
    with pytest.raises(V.VerifierUnavailable, match="identity"):
        V.lean_check("theorem truth : True := by trivial", {
            "lean": V.sys.executable,
        })


def test_lean_check_returns_bounded_kernel_diagnostic(monkeypatch):
    responses = iter((
        (0, "Lean (version 4.19.0)"),
        (1, "x" * 9000 + "\nMain.lean:1: error: type mismatch"),
    ))
    monkeypatch.setattr(V, "_run", lambda *args, **kwargs: next(responses))

    verdict = V.lean_check(
        "theorem bad : False := by trivial", {"lean": V.sys.executable},
    )

    assert verdict.passed is False
    assert "type mismatch" in verdict.reason
    assert len(verdict.detail) <= 8000


def test_lean_check_uses_a_pinned_lake_project(monkeypatch, tmp_path):
    (tmp_path / "lakefile.toml").write_text(
        'name = "formal-test"\n', encoding="utf-8",
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((tuple(command), kwargs))
        assert command[:3] == [V.sys.executable, "env", V.sys.executable]
        assert kwargs["cwd"] == str(tmp_path)
        if command[-1] == "--version":
            return 0, "Lean (version 4.34.0, x86_64-unknown-linux-gnu)"
        assert open(command[-1], encoding="utf-8").read().startswith("import Mathlib")
        return 0, ""

    monkeypatch.setattr(V, "_run", fake_run)
    verdict = V.lean_check(
        "import Mathlib\nexample (n : ℕ) : n + 0 = n := by simp\n",
        {
            "lean": V.sys.executable,
            "lake": V.sys.executable,
            "project": str(tmp_path),
        },
    )

    assert verdict.passed is True
    assert len(calls) == 2
    assert not os.path.exists(calls[-1][0][-1])


def test_lean_check_reads_formal_toolchain_defaults_from_environment(
    monkeypatch, tmp_path,
):
    (tmp_path / "lakefile.lean").write_text("package Formal\n", encoding="utf-8")
    monkeypatch.setenv("SONDER_LEAN_EXE", V.sys.executable)
    monkeypatch.setenv("SONDER_LAKE_EXE", V.sys.executable)
    monkeypatch.setenv("SONDER_LEAN_PROJECT", str(tmp_path))
    responses = iter(((0, "Lean (version 4.34.0)"), (0, "")))
    monkeypatch.setattr(V, "_run", lambda *args, **kwargs: next(responses))

    assert V.lean_check("theorem truth : True := by trivial").passed is True


def test_lean_check_rejects_a_non_lake_project(tmp_path):
    with pytest.raises(V.VerifierUnavailable, match="no lakefile"):
        V.lean_check(
            "theorem truth : True := by trivial",
            {"lean": V.sys.executable, "project": str(tmp_path)},
        )


def test_lean_check_applies_and_verifies_the_repository_pin(monkeypatch):
    calls = []
    monkeypatch.delenv("SONDER_LEAN_EXE", raising=False)
    monkeypatch.delenv("SONDER_LEAN_PROJECT", raising=False)
    monkeypatch.setattr(V.shutil, "which", lambda value: "/fake/lean")

    def fake_run(command, **kwargs):
        calls.append((tuple(command), kwargs))
        cwd = kwargs["cwd"]
        assert open(os.path.join(cwd, "lean-toolchain"), encoding="utf-8").read() == (
            "leanprover/lean4:v4.34.0\n"
        )
        if "--version" in command:
            return 0, "Lean (version 4.34.0, x86_64-unknown-linux-gnu)"
        return 0, ""

    monkeypatch.setattr(V, "_run", fake_run)
    assert V.lean_check("theorem truth : True := by trivial").passed is True
    assert len(calls) == 2


def test_lean_check_fails_closed_when_default_lean_ignores_the_pin(monkeypatch):
    monkeypatch.delenv("SONDER_LEAN_EXE", raising=False)
    monkeypatch.delenv("SONDER_LEAN_PROJECT", raising=False)
    monkeypatch.setattr(V.shutil, "which", lambda value: "/fake/lean")
    monkeypatch.setattr(
        V,
        "_run",
        lambda *args, **kwargs: (0, "Lean (version 4.33.0)"),
    )

    with pytest.raises(V.VerifierUnavailable, match="does not match repository pin"):
        V.lean_check("theorem truth : True := by trivial")


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
