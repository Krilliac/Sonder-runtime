"""Issue #510 guard canaries: no-progress verification loop in codegen.

``codegen_build_loop`` spends at least one ensemble generation plus a full
build per attempt.  The canaries drive the real server entry point with a
fake compiler that keeps returning the same errors (the loop is stuck) or an
absurd attempt count (the loop is runaway) and count the model requests that
actually happened.  The normal-traffic tests prove a converging loop and the
default two-attempt contract are untouched.
"""
from __future__ import annotations

import server
from sonder_runtime.domain import verification_progress as vp


def _build(ok, stdout):
    return {
        "ok": ok, "program": "build", "command": "build", "cwd": ".",
        "returncode": 0 if ok else 1, "timed_out": False, "elapsed_ms": 1,
        "stdout": stdout, "stderr": "",
        "stdout_truncated": False, "stderr_truncated": False,
    }


def _prepare(monkeypatch, tmp_path, outcomes):
    """``outcomes(n)`` gives the n-th build's (ok, stdout); n counts from 1."""
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    builds = []

    def run_program(*args, **kwargs):
        builds.append(1)
        return _build(*outcomes(len(builds)))

    monkeypatch.setattr(server.workbench, "run_program", run_program)
    asked = []

    def fake_ensemble(prompt, **kwargs):
        asked.append(prompt)
        return "int main(void) { return %d; }" % len(asked)

    monkeypatch.setattr(server, "ensemble_answer", fake_ensemble)
    return asked, builds


def test_canary_identical_build_errors_stop_regeneration(monkeypatch, tmp_path):
    stuck = "main.c:1: error: unknown type name 'widget'"
    asked, _builds = _prepare(monkeypatch, tmp_path, lambda n: (False, stuck))

    out = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build", attempts=5,
    )

    # Two identical failing attempts are enough evidence; attempts 3-5 would
    # have re-sent the same prompt to the ensemble for the same verdict.
    assert len(asked) == 2
    assert "no progress" in out
    assert vp.outcome_fingerprint([stuck]) in out
    assert "BUILD SUCCEEDED" not in out


def test_canary_runaway_attempt_count_is_clamped(monkeypatch, tmp_path):
    # Every build fails differently, so only the attempt cap can stop it.
    asked, _builds = _prepare(
        monkeypatch, tmp_path,
        lambda n: (False, "main.c:%d: error: moving target %d" % (n, n)),
    )

    out = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build", attempts=1000,
    )

    assert len(asked) == vp.MAX_VERIFICATION_ATTEMPTS
    assert "no progress" not in out


def test_normal_converging_loop_is_not_cut_short(monkeypatch, tmp_path):
    # initial build, attempt 1, attempt 2 fail with different errors; the
    # third attempt's build (build #4) and the final build (#5) are clean.
    def outcomes(n):
        if n <= 3:
            return False, "main.c:%d: error: still wrong %d" % (n, n)
        return True, "ok"

    asked, _builds = _prepare(monkeypatch, tmp_path, outcomes)

    out = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build", attempts=5,
    )

    assert len(asked) == 3
    assert "no progress" not in out
    assert "BUILD SUCCEEDED" in out


def test_normal_default_two_attempts_are_unchanged(monkeypatch, tmp_path):
    stuck = "main.c:1: error: same"
    asked, _builds = _prepare(monkeypatch, tmp_path, lambda n: (False, stuck))

    server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build",
    )

    assert len(asked) == 2


def test_guard_resets_on_change_and_never_stalls_on_clean():
    guard = vp.VerificationProgressGuard()
    assert guard.observe(["a: error x"]) is False
    assert guard.observe(["a: error y"]) is False
    assert guard.observe([]) is False
    assert guard.observe([]) is False
    assert guard.observe(["a: error y"]) is False
    # Order and whitespace do not make an identical outcome look new.
    assert guard.observe(["  a:   error y "]) is True
    assert guard.stalled_fingerprint == vp.outcome_fingerprint(["a: error y"])


def test_bounded_attempts_rejects_nonsense():
    assert vp.bounded_attempts(0) == 1
    assert vp.bounded_attempts(-4) == 1
    assert vp.bounded_attempts("3") == 3
    assert vp.bounded_attempts("lots") == 2
    assert vp.bounded_attempts(True) == 2
    assert vp.bounded_attempts(10**9) == vp.MAX_VERIFICATION_ATTEMPTS
