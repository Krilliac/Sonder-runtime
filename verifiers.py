"""verifiers — a pluggable grounding registry so the generate->verify->repair
principle applies BEYOND Python games.

The insight from the game gauntlet: solver.solve(), run_ladder_repair(), and
reward.record_outcome() are all *verifier-agnostic* — they take the pass/fail
oracle as an injected dependency. So "does grounding apply everywhere?" reduces
to "how many verifier backends have we registered?". Each backend maps a produced
artifact + a task spec to a Verdict; wiring a new domain = adding one function
here, not touching the loops. solver.solve_verified() is the single seam that
drives self-repair off any registered verifier.

A verifier: fn(artifact: str, spec: dict) -> Verdict(passed, reason, detail)
  artifact — the model's output (code, a program, a patch)
  spec     — task context, verifier-specific (documented per backend below)
  detail   — the FULL diagnostic (traceback/compiler output) for the repair loop;
             `reason` is the one-line summary for logging.

Raises VerifierUnavailable when a backend's external tool (compiler, mypy) is
absent — that is "could not judge", distinct from a Verdict(False) "artifact failed".
"""
import collections
import os
import subprocess
import sys
import tempfile

import grounding

Verdict = collections.namedtuple("Verdict", ["passed", "reason", "detail"])

# Genuinely future backends (documented surface, not yet implemented).
PLANNED = {
    "fuzz": "run a fuzzer against the artifact; passed iff no crash within budget",
    "benchmark_perf": "run + time the artifact; passed iff within a perf threshold",
}

_VCVARS = (r"C:\Program Files\Microsoft Visual Studio\2022\Community\VC"
           r"\Auxiliary\Build\vcvars64.bat")


class VerifierUnavailable(RuntimeError):
    """The verifier's external tool isn't present — 'could not judge', not 'failed'."""


def _last_line(text):
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1] if lines else ""


def _run(cmd, cwd=None, timeout=180, shell=False):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout, shell=shell)
    out = ((p.stdout or b"").decode("utf-8", "replace")
           + (p.stderr or b"").decode("utf-8", "replace"))
    return p.returncode, out


# --- python: execute code + an assert-check --------------------------------
def python_exec(artifact, spec=None):
    """spec={'check': <assert lines>}. Runs code+check in a subprocess."""
    check = (spec or {}).get("check", "")
    ok, out = grounding.run_code(artifact, check)
    return Verdict(ok, "passed" if ok else (_last_line(out) or "failed"), out)


# --- program: run a whole program headless, fail on crash ------------------
def program_run(artifact, spec=None):
    """spec={'kind': 'console'|'pygame'}. Runs the program; fails on real crash."""
    import game_ladder  # local import avoids an import-time cycle
    kind = (spec or {}).get("kind", "console")
    passed, reason, full = game_ladder._ground_capture(artifact, kind)
    return Verdict(passed, reason, full)


# --- pytest: run a repo's tests --------------------------------------------
def pytest_run(artifact, spec=None):
    """spec={'cwd': dir, 'select': nodeid?, 'write_to': path?, 'python': exe?}.
    If write_to is given, the artifact is written there first (module under test)."""
    spec = spec or {}
    if spec.get("write_to") and artifact:
        with open(spec["write_to"], "w", encoding="utf-8") as f:
            f.write(artifact)
    interp = spec.get("python", sys.executable)
    args = [interp, "-m", "pytest", "-q"]
    if spec.get("select"):
        args.append(spec["select"])
    rc, out = _run(args, cwd=spec.get("cwd") or ".", timeout=spec.get("timeout", 300))
    return Verdict(rc == 0, "passed" if rc == 0 else (_last_line(out) or "pytest failed"),
                   out[-4000:])


# --- typecheck: mypy as a cheap partial oracle -----------------------------
def typecheck(artifact, spec=None):
    """spec={'python': exe?}. Runs mypy on the artifact; VerifierUnavailable if mypy absent."""
    interp = (spec or {}).get("python", sys.executable)
    fd, path = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(artifact)
        rc, out = _run([interp, "-m", "mypy", "--no-error-summary", "--no-color-output", path],
                       timeout=120)
        if "No module named mypy" in out or "No module named 'mypy'" in out:
            raise VerifierUnavailable("mypy not installed")
        return Verdict(rc == 0, "passed" if rc == 0 else (_last_line(out) or "type errors"),
                       out[-4000:])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# --- cpp: compile a single translation unit via MSVC/vcvars ----------------
def cpp_compile(artifact, spec=None):
    """spec={'vcvars': path?, 'std': 'c++17'?}. Compile-only (/c) via vcvars;
    VerifierUnavailable if vcvars64.bat is missing."""
    spec = spec or {}
    vcvars = spec.get("vcvars", _VCVARS)
    if not os.path.exists(vcvars):
        raise VerifierUnavailable("vcvars64.bat not found at %s" % vcvars)
    d = tempfile.mkdtemp()
    src = os.path.join(d, "tu.cpp")
    with open(src, "w", encoding="utf-8") as f:
        f.write(artifact)
    std = spec.get("std", "c++17")
    # Run through a .bat: `cmd /c "call \"path with spaces\" && cl ..."` gets its
    # outer quotes stripped by cmd and mangles the vcvars path — a wrapper file dodges it.
    bat = os.path.join(d, "build.bat")
    with open(bat, "w", encoding="utf-8") as f:
        f.write('@echo off\r\ncall "%s" >nul\r\ncl /nologo /EHsc /std:%s /c "%s"\r\n'
                % (vcvars, std, src))
    rc, out = _run(["cmd", "/c", bat], cwd=d, timeout=spec.get("timeout", 180))
    if rc == 0:
        reason = "compiled"
    else:
        # prefer the real MSVC diagnostic over trailing vcvars noise (vswhere, etc.)
        errs = [l.strip() for l in out.splitlines() if "): error" in l or "error C" in l]
        reason = errs[0] if errs else (_last_line(out) or "compile error")
    return Verdict(rc == 0, reason, out[-4000:])


# --- llm_judge: model-graded rubric for non-executable outputs -------------
def llm_judge(artifact, spec=None):
    """spec={'rubric': str, 'threshold': int 0-10, 'judge_fn': callable?}. Weak
    oracle for outputs with no executable check (design, prose). judge_fn(prompt)
    -> text is injectable; defaults to the local trilobite model."""
    import re
    spec = spec or {}
    rubric = spec.get("rubric", "Is this a correct, complete, high-quality answer?")
    threshold = spec.get("threshold", 7)
    judge_fn = spec.get("judge_fn")
    if judge_fn is None:
        import server
        model = server.resolve_trilobite_model(False)
        judge_fn = server._make_generate(
            model, "You are a strict grader. Reply with one integer 0-10, then a brief reason.",
            0.0, 256, 4096)
    resp = judge_fn("RUBRIC: %s\n\nOUTPUT TO GRADE:\n%s\n\nScore 0-10 (integer first):"
                    % (rubric, artifact)) or ""
    m = re.search(r"\d+", resp)
    score = int(m.group()) if m else 0
    return Verdict(score >= threshold, "judge %d/%d" % (score, threshold), resp)


REGISTRY = {
    "python_exec": python_exec,
    "program_run": program_run,
    "pytest_run": pytest_run,
    "typecheck": typecheck,
    "cpp_compile": cpp_compile,
    "llm_judge": llm_judge,
}


def get(name):
    if name not in REGISTRY:
        raise KeyError("no verifier %r (have %s; planned %s)"
                       % (name, sorted(REGISTRY), sorted(PLANNED)))
    return REGISTRY[name]


def verify(name, artifact, spec=None):
    """The single seam solver/ladder/reward call. Adding a domain never touches them."""
    return get(name)(artifact, spec)
