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
import shutil
import subprocess
import sys
import tempfile

import grounding
import sonder_logging
from sonder_runtime.adapters.execution_tools import code_runner

Verdict = collections.namedtuple("Verdict", ["passed", "reason", "detail"])

# Genuinely future backends (documented surface, not yet implemented).
PLANNED = {
    "fuzz": "run a fuzzer against the artifact; passed iff no crash within budget",
    "benchmark_perf": "run + time the artifact; passed iff within a perf threshold",
}

# cpp_compile interpolates these into an executed .bat, so they are validated:
_ALLOWED_CPP_STD = {"c++11", "c++14", "c++17", "c++20", "c++23", "c++latest"}
_BAT_META = set('&|<>^"%\r\n')


class VerifierUnavailable(RuntimeError):
    """The verifier's external tool isn't present — 'could not judge', not 'failed'."""


# What the OS/shell prints when it cannot find the executable at all, as opposed
# to the executable running and rejecting the artifact. Same list node_verifier
# uses (node_verifier.py:27) — the two backends face the same distinction.
_TOOL_MISSING_MARKERS = (
    "is not recognized as an internal or external command",  # Windows cmd
    "No such file or directory",  # POSIX
    "command not found",
)


def _last_line(text):
    lines = [row for row in (text or "").strip().splitlines() if row.strip()]
    return lines[-1] if lines else ""


def _run(cmd, cwd=None, timeout=180, shell=False):
    p = subprocess.run(
        cmd, cwd=cwd, capture_output=True, timeout=timeout, shell=shell,
        env=sonder_logging.child_environment(),
    )
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
    cwd = spec.get("cwd") or "."
    write_to = spec.get("write_to")
    if write_to and artifact:
        # confine the write under cwd — reject traversal / absolute-path escapes
        base = os.path.abspath(cwd)
        dest = os.path.abspath(os.path.join(base, write_to))
        try:
            inside = os.path.commonpath([base, dest]) == base
        except ValueError:  # different drive on Windows
            inside = False
        if not inside:
            raise ValueError("write_to escapes cwd: %r" % (write_to,))
        with open(dest, "w", encoding="utf-8") as f:
            f.write(artifact)
    interp = spec.get("python", sys.executable)
    args = [interp, "-m", "pytest", "-q"]
    select = spec.get("select")
    if select:
        if str(select).startswith("-"):
            raise ValueError("select must be a test path/nodeid, not an option: %r" % (select,))
        args.append(str(select))
    rc, out = _run(args, cwd=cwd, timeout=spec.get("timeout", 300))
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
    vcvars = spec.get("vcvars") or code_runner._find_visual_studio_vcvars()
    if not vcvars:
        raise VerifierUnavailable("vcvars64.bat was not discovered")
    vcvars = os.fspath(vcvars)
    # vcvars is interpolated into a batch `call`; require a real file with no shell
    # metacharacters to block command injection via a crafted spec['vcvars'].
    if not os.path.isfile(vcvars) or (_BAT_META & set(vcvars)):
        raise VerifierUnavailable("vcvars64.bat not found or unsafe path: %r" % (vcvars,))
    std = spec.get("std", "c++17")
    # std is also interpolated into the batch line — allowlist it (no injection).
    if std not in _ALLOWED_CPP_STD:
        raise ValueError("unsupported /std %r (allowed: %s)" % (std, sorted(_ALLOWED_CPP_STD)))
    d = tempfile.mkdtemp()
    try:
        src = os.path.join(d, "tu.cpp")  # our own mkdtemp path — not caller-controlled
        with open(src, "w", encoding="utf-8") as f:
            f.write(artifact)
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
            errs = [row.strip() for row in out.splitlines()
                    if "): error" in row or "error C" in row]
            if not errs and any(m in out for m in _TOOL_MISSING_MARKERS):
                # vcvars64.bat exists but there is no x64 toolset behind it (the
                # VC.CoreBuildTools case), or vcvars aborted and its diagnostic
                # went to the `>nul` above: cmd exits 9009 having printed only
                # its own not-found message. With no MSVC diagnostic to quote,
                # this returned Verdict(False, "operable program or batch file.")
                # — "could not judge" reported as "the artifact FAILED", so every
                # C++ artifact including correct ones was judged failed and fed
                # to solver's repair loop, burning the repair budget and writing
                # false-negative reward rows. The isfile() guard above only
                # covers the "no Visual Studio at all" case.
                raise VerifierUnavailable(
                    "MSVC cl.exe not usable via %r: %s" % (vcvars, _last_line(out)))
            reason = errs[0] if errs else (_last_line(out) or "compile error")
        return Verdict(rc == 0, reason, out[-4000:])
    finally:
        # Nothing removed this directory on ANY path — success, compile failure,
        # or the TimeoutExpired _run propagates — and it holds tu.cpp, build.bat
        # and any .obj. The test suite alone had left 192 of them in %TEMP%.
        shutil.rmtree(d, ignore_errors=True)


# --- llm_judge: model-graded rubric for non-executable outputs -------------
def llm_judge(artifact, spec=None):
    """spec={'rubric': str, 'threshold': int 0-10, 'judge_fn': callable?}. Weak
    oracle for outputs with no executable check (design, prose). judge_fn(prompt)
    -> text is injectable; defaults to the local model selected by Sonder Runtime."""
    import re
    spec = spec or {}
    rubric = spec.get("rubric", "Is this a correct, complete, high-quality answer?")
    threshold = spec.get("threshold", 7)
    judge_fn = spec.get("judge_fn")
    if judge_fn is None:
        import server
        model = server.resolve_sonder_model(False)
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


# External verifier backends promoted from the improvement fleet. Registered
# defensively — a missing/broken ext module never breaks the core registry.
#
# Their Verdict compatibility is STRUCTURAL, not by shared class: node_verifier
# does `from verifiers import Verdict, VerifierUnavailable`, but sql_verifier and
# json_schema_verifier each define their own Verdict namedtuple, so
# `isinstance(v, verifiers.Verdict)` is False for those two. That is fine — the
# only Verdict surface solver/ladder/reward touch is the .passed/.reason/.detail
# fields, which every backend has.
#
# The exception class is NOT interchangeable that way. A backend that signals
# "could not judge" MUST raise THIS module's VerifierUnavailable, or an
# `except verifiers.VerifierUnavailable` around verify() silently misses it —
# a same-named local subclass of RuntimeError is a different type. sql_verifier
# and json_schema_verifier have no external tool and never raise it at all.
# test_verifiers.py pins that rule for every promoted backend.
def _register_ext(mod_name, fn_name):
    """Resolve a promoted backend's entry point, tolerating the circular-import
    window. A backend that imports from `verifiers` at module scope (node_verifier
    does) is only half-initialized while THIS module executes, so `fn_name` may
    not be bound on it yet and an eager getattr silently drops the backend —
    `import node_verifier` before `import verifiers` used to leave "node_run"
    missing from REGISTRY entirely. Bind a late-resolving shim in exactly that
    window; a genuinely absent or broken module still raises out and stays
    unregistered, so the core registry is unaffected either way."""
    mod = __import__(mod_name)
    fn = getattr(mod, fn_name, None)
    if fn is not None:
        return fn
    if not getattr(getattr(mod, "__spec__", None), "_initializing", False):
        raise AttributeError("module %r has no %r" % (mod_name, fn_name))

    def _late(artifact, spec=None):
        return getattr(sys.modules[mod_name], fn_name)(artifact, spec)

    _late.__name__ = fn_name
    _late.__doc__ = "late-bound %s.%s (mid circular import; resolved on first call)" % (
        mod_name, fn_name)
    return _late


for _key, _mod, _fn in (
    ("node_run", "node_verifier", "node_run"),
    ("sql_valid", "sql_verifier", "sql_valid"),
    ("json_schema", "json_schema_verifier", "json_schema_verify"),
    ("ruff_check", "ruff_verifier", "ruff_check"),
):
    try:
        REGISTRY[_key] = _register_ext(_mod, _fn)
    except Exception:
        pass
