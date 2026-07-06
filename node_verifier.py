"""node_verifier — Node.js execution backend for trilobite's verifiers registry.

Mirrors verifiers.python_exec / verifiers.cpp_compile but grounds JavaScript
artifacts: writes the artifact (plus an optional appended assertion-style
`check`) to a temp .js file and actually runs it with a real `node`
subprocess, so pass/fail comes from execution rather than a model's own
say-so. Wire it into the registry with one line, no changes to solver/
reward/ladder:

    import verifiers, node_verifier
    verifiers.REGISTRY["node_run"] = node_verifier.node_run

Raises VerifierUnavailable when `node` isn't installed/on PATH — "could not
judge", distinct from Verdict(False) "artifact failed" (same contract as
every other backend in verifiers.py).
"""
import os
import subprocess
import tempfile

from verifiers import Verdict, VerifierUnavailable

# Substrings seen when the shell/OS couldn't find the `node` executable at
# all (as opposed to node running and the *script* failing). Covers both
# subprocess raising FileNotFoundError directly and the rarer case where a
# wrapping shell prints one of these instead of raising.
_NOT_FOUND_MARKERS = (
    "is not recognized as an internal or external command",  # Windows cmd
    "No such file or directory",  # POSIX
    "command not found",
)


def _last_line(text):
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1] if lines else ""


def _run(cmd, cwd=None, timeout=15):
    """Run `cmd`, returning (returncode, combined stdout+stderr). Left as a
    thin, monkeypatchable seam (like verifiers._run) so tests never need a
    real `node` binary. May raise FileNotFoundError or
    subprocess.TimeoutExpired — node_run() translates both."""
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)
    out = ((p.stdout or b"").decode("utf-8", "replace")
           + (p.stderr or b"").decode("utf-8", "replace"))
    return p.returncode, out


def node_run(artifact, spec=None):
    """spec={'check': <JS appended after the artifact>?, 'node': exe name/
    path (default 'node')?, 'timeout': seconds (default 15)?}.

    Writes artifact(+check) to a temp .js file and executes it with `node`;
    passed iff the process exits 0. Raises VerifierUnavailable if node can't
    be found on PATH.
    """
    spec = spec or {}
    node_exe = spec.get("node", "node")
    timeout = spec.get("timeout", 15)
    check = spec.get("check", "")
    src = (artifact or "") + (("\n\n" + check) if check else "")

    fd, path = tempfile.mkstemp(suffix=".js")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        try:
            rc, out = _run([node_exe, path], timeout=timeout)
        except FileNotFoundError:
            raise VerifierUnavailable("node executable not found (%r)" % node_exe)
        except subprocess.TimeoutExpired:
            return Verdict(False, "timed out after %ss" % timeout, "(timed out after %ss)" % timeout)
        if any(m in out for m in _NOT_FOUND_MARKERS):
            raise VerifierUnavailable(
                "node executable not found (%r): %s" % (node_exe, out.strip()))
        return Verdict(rc == 0,
                        "passed" if rc == 0 else (_last_line(out) or "node exited nonzero"),
                        out[-4000:])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
