import subprocess

import pytest

import node_verifier as NV
from verifiers import Verdict, VerifierUnavailable


# --- happy path: node runs cleanly, exits 0 --------------------------------
def test_node_run_pass(monkeypatch):
    monkeypatch.setattr(NV, "_run", lambda *a, **k: (0, ""))
    v = NV.node_run("console.log('hi');")
    assert isinstance(v, Verdict)
    assert v.passed is True
    assert v.reason == "passed"


# --- artifact throws / exits nonzero ---------------------------------------
def test_node_run_fail_reports_last_line(monkeypatch):
    stderr = (
        "/tmp/x.js:1\n"
        "throw new Error('boom');\n"
        "^\n\n"
        "Error: boom\n"
        "    at Object.<anonymous> (/tmp/x.js:1:7)"
    )
    monkeypatch.setattr(NV, "_run", lambda *a, **k: (1, stderr))
    v = NV.node_run("throw new Error('boom');")
    assert v.passed is False
    assert "at Object" in v.reason
    assert "boom" in v.detail


# --- check is appended after the artifact, and both are actually run ------
def test_node_run_appends_check(monkeypatch):
    captured = {}

    def fake_run(cmd, cwd=None, timeout=15):
        with open(cmd[1], "r", encoding="utf-8") as f:
            captured["src"] = f.read()
        return (0, "")

    monkeypatch.setattr(NV, "_run", fake_run)
    v = NV.node_run("function f() { return 1; }",
                    {"check": "if (f() !== 1) throw new Error('bad');"})
    assert v.passed is True
    assert "function f()" in captured["src"]
    assert "if (f() !== 1)" in captured["src"]
    # artifact must come before the check in the concatenated source
    assert captured["src"].index("function f()") < captured["src"].index("if (f()")


# --- node missing: real FileNotFoundError from subprocess ------------------
def test_node_run_unavailable_when_node_missing(monkeypatch):
    def raise_not_found(cmd, cwd=None, timeout=15):
        raise FileNotFoundError("node not found")

    monkeypatch.setattr(NV, "_run", raise_not_found)
    with pytest.raises(VerifierUnavailable):
        NV.node_run("console.log(1);")


# --- node missing: shell-reported "not recognized" text instead of raise --
def test_node_run_unavailable_on_not_recognized_text(monkeypatch):
    monkeypatch.setattr(
        NV, "_run",
        lambda *a, **k: (1, "'node' is not recognized as an internal or external command"))
    with pytest.raises(VerifierUnavailable):
        NV.node_run("console.log(1);")


# --- timeout: treated as a failed verdict, not Unavailable, not a crash ----
def test_node_run_timeout_is_a_failed_verdict(monkeypatch):
    def raise_timeout(cmd, cwd=None, timeout=15):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(NV, "_run", raise_timeout)
    v = NV.node_run("while (true) {}", {"timeout": 5})
    assert v.passed is False
    assert "timed out" in v.reason


# --- spec.node lets callers point at a specific interpreter/path -----------
def test_node_run_uses_spec_node_path(monkeypatch):
    seen = {}

    def fake_run(cmd, cwd=None, timeout=15):
        seen["cmd"] = cmd
        seen["timeout"] = timeout
        return (0, "")

    monkeypatch.setattr(NV, "_run", fake_run)
    v = NV.node_run("1;", {"node": r"C:\tools\node\node.exe", "timeout": 42})
    assert v.passed is True
    assert seen["cmd"][0] == r"C:\tools\node\node.exe"
    assert seen["timeout"] == 42


# --- registry-compatibility: return type matches the shared Verdict shape -
def test_node_run_returns_shared_verdict_namedtuple(monkeypatch):
    monkeypatch.setattr(NV, "_run", lambda *a, **k: (0, ""))
    v = NV.node_run("1;")
    assert v._fields == ("passed", "reason", "detail")
