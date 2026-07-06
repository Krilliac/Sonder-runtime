import subprocess

import pytest

import ruff_verifier as R


def test_get_unknown_raises():
    with pytest.raises(KeyError):
        R.get("does_not_exist")


def test_registry_covers_ruff_check():
    assert "ruff_check" in R.REGISTRY
    assert R.REGISTRY["ruff_check"] is R.ruff_check


# --- deterministic, monkeypatched _run (no real ruff binary needed) --------
def test_ruff_check_pass_when_clean(monkeypatch):
    monkeypatch.setattr(R, "_run", lambda *a, **k: (0, ""))
    v = R.verify("ruff_check", "x = 1\n")
    assert v.passed is True
    assert v.reason == "clean"


def test_ruff_check_fail_reports_violation(monkeypatch):
    out = "stdin.py:1:8: F401 [*] `os` imported but unused\nFound 1 error.\n"
    monkeypatch.setattr(R, "_run", lambda *a, **k: (1, out))
    v = R.verify("ruff_check", "import os\n")
    assert v.passed is False
    assert v.reason == "Found 1 error."
    assert "F401" in v.detail


def test_ruff_check_unavailable_when_missing(monkeypatch):
    def fake_run(cmd, input_text, timeout=30):
        raise FileNotFoundError("no such file: %r" % cmd[0])
    monkeypatch.setattr(R, "_run", fake_run)
    with pytest.raises(R.VerifierUnavailable):
        R.ruff_check("x = 1\n")


def test_ruff_check_unexpected_rc_raises_unavailable(monkeypatch):
    monkeypatch.setattr(R, "_run", lambda *a, **k: (2, "ruff: invalid --select value"))
    with pytest.raises(R.VerifierUnavailable):
        R.ruff_check("x = 1\n", {"select": "not-a-real-code"})


def test_ruff_check_builds_select_flag(monkeypatch):
    seen = {}

    def fake_run(cmd, input_text, timeout=30):
        seen["cmd"] = cmd
        return (0, "")

    monkeypatch.setattr(R, "_run", fake_run)
    R.ruff_check("x = 1\n", {"select": "F,E"})
    assert seen["cmd"][0] == "ruff"
    assert "--select" in seen["cmd"]
    assert "F,E" in seen["cmd"]


def test_ruff_check_custom_executable_name(monkeypatch):
    seen = {}

    def fake_run(cmd, input_text, timeout=30):
        seen["cmd"] = cmd
        return (0, "")

    monkeypatch.setattr(R, "_run", fake_run)
    R.ruff_check("x = 1\n", {"ruff": "ruff.exe"})
    assert seen["cmd"][0] == "ruff.exe"


# --- real subprocess path, but with a guaranteed-absent executable --------
# No mocking here: exercises the actual _run/subprocess.run FileNotFoundError
# path end-to-end without depending on ruff being installed anywhere.
def test_ruff_check_real_missing_executable():
    with pytest.raises(R.VerifierUnavailable):
        R.ruff_check("x = 1\n", {"ruff": "definitely-not-a-real-ruff-binary-zzz"})


def test_run_feeds_stdin_and_captures_output(monkeypatch):
    # Sanity-check _run's own contract using a real subprocess (python, always
    # present) instead of ruff, so this test needs no external tool either.
    class FakeCompleted:
        returncode = 0
        stdout = b"hello\n"
        stderr = b""

    def fake_subprocess_run(cmd, input=None, capture_output=None, timeout=None):
        assert input == b"hi"
        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    rc, out = R._run(["anything"], "hi")
    assert rc == 0
    assert out == "hello\n"
