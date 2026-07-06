"""ruff_verifier — a lint-gate verifier backend for the `verifiers` registry.

Mirrors the existing backends in verifiers.py (typecheck, cpp_compile, ...):
a verifier is `fn(artifact, spec) -> Verdict(passed, reason, detail)`, and it
raises VerifierUnavailable ("could not judge") instead of returning a failing
Verdict when its external tool is missing. That distinction matters to the
solver's self-repair loop: a missing tool must not look like a bad artifact
worth "fixing".

ruff_check runs `ruff check -` (stdin mode) over a code string and turns the
process exit code into a Verdict:
  rc == 0            -> passed, no violations
  rc == 1            -> failed, violations found (detail = ruff's own output,
                        already formatted for a human/model to read and act on)
  anything else, or
  the executable missing -> VerifierUnavailable

This is deliberately a *style* gate, not a correctness oracle — it composes
with python_exec/pytest_run rather than replacing them (e.g. run ruff_check
first as a cheap pre-filter, then the expensive real-execution verifier).
"""
import collections
import subprocess

Verdict = collections.namedtuple("Verdict", ["passed", "reason", "detail"])


class VerifierUnavailable(RuntimeError):
    """ruff isn't runnable here — 'could not judge', not 'artifact failed'."""


def _last_line(text):
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1] if lines else ""


def _run(cmd, input_text, timeout=30):
    """Runs cmd, feeding input_text on stdin. Isolated so tests can monkeypatch
    it the same way tests/test_verifiers.py monkeypatches typecheck's _run —
    no real `ruff` binary is required to exercise ruff_check's logic."""
    p = subprocess.run(cmd, input=(input_text or "").encode("utf-8"),
                        capture_output=True, timeout=timeout)
    out = ((p.stdout or b"").decode("utf-8", "replace")
           + (p.stderr or b"").decode("utf-8", "replace"))
    return p.returncode, out


def ruff_check(code, spec=None):
    """spec={'ruff': exe?, 'select': 'F,E'?, 'timeout': int?}.
    Runs `ruff check -` over `code` via stdin. VerifierUnavailable if the
    ruff executable can't be found or the invocation itself errors out
    (as opposed to a clean run that simply found violations)."""
    spec = spec or {}
    exe = spec.get("ruff", "ruff")
    cmd = [exe, "check", "--no-cache", "-"]
    select = spec.get("select")
    if select:
        cmd += ["--select", select]
    try:
        rc, out = _run(cmd, code, timeout=spec.get("timeout", 30))
    except FileNotFoundError:
        raise VerifierUnavailable("ruff not installed (executable %r not found)" % exe)
    if rc not in (0, 1):
        raise VerifierUnavailable(
            "ruff invocation failed (rc=%d): %s" % (rc, _last_line(out) or "unexpected error"))
    passed = rc == 0
    reason = "clean" if passed else (_last_line(out) or "lint errors")
    return Verdict(passed, reason, out)


REGISTRY = {
    "ruff_check": ruff_check,
}


def get(name):
    if name not in REGISTRY:
        raise KeyError("no verifier %r (have %s)" % (name, sorted(REGISTRY)))
    return REGISTRY[name]


def verify(name, artifact, spec=None):
    """Same seam as verifiers.verify — solver/ladder/reward can call either
    registry interchangeably, or the two can be merged with `dict.update`."""
    return get(name)(artifact, spec)
