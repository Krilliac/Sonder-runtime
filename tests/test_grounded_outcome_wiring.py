"""The verification tools hand the ledger what actually happened, not just `ok`.

``harness_tools`` already distinguishes "the tests failed" from "the tests never
ran": a timeout and a missing binary both come back with ``returncode: -1``, and
the build-system detector returns an ``error`` dict without spawning anything.
The MCP wrappers used to forward only ``data["ok"]``, so all three were filed as
signal ``failed`` (-1.0) against whatever generation was pending -- the harshest
signal in the table, for evidence that does not exist.

These tests are about the wiring specifically. The decision itself is tested in
``test_grounded_outcomes.py``; what is easy to break here is a call site that
quietly stops passing the evidence along.
"""
import pytest

import grounded_outcomes as go
import server


VERIFIERS = (
    ("test_run", "test_run"),
    ("lint_run", "lint_run"),
    ("typecheck_run", "typecheck_run"),
    ("build_run", "build_run"),
)

# What harness_tools returns when the verification never produced a verdict.
NEVER_RAN = (
    {"ok": False, "returncode": -1, "timed_out": True, "stdout": "", "stderr": "",
     "elapsed_ms": 120000, "command": ["pytest"]},
    {"ok": False, "returncode": -1, "timed_out": False, "stdout": "",
     "stderr": "command not found: pytest", "elapsed_ms": 1, "command": ["pytest"]},
    {"ok": False, "error": "no recognized build system found at /x"},
)

REALLY_FAILED = {
    "ok": False, "returncode": 1, "timed_out": False, "stdout": "2 failed",
    "stderr": "", "elapsed_ms": 900, "command": ["pytest"],
}


@pytest.fixture
def ledger(monkeypatch):
    """A pending generation plus a capture of every outcome row written."""
    go.reset()
    written = []
    monkeypatch.setattr(
        server, "_record_outcome_signal",
        lambda ident, signal: written.append((ident, signal)),
    )
    go.note_generation("i1", "sonder")
    yield written
    go.reset()


def _run(monkeypatch, tool, data):
    monkeypatch.setattr(server.harness_tools, tool, lambda **_kwargs: data)
    getattr(server, tool)()


@pytest.mark.parametrize("tool,harness_name", VERIFIERS)
@pytest.mark.parametrize("data", NEVER_RAN)
def test_a_verification_that_never_ran_writes_no_outcome(
    monkeypatch, ledger, tool, harness_name, data,
):
    _run(monkeypatch, harness_name, data)

    assert ledger == [], "%s filed a verdict it never obtained" % tool
    assert go.pending_count() == 1, "the generation is still awaiting real evidence"


@pytest.mark.parametrize("tool,harness_name", VERIFIERS)
def test_a_verification_that_really_failed_still_writes_the_failure(
    monkeypatch, ledger, tool, harness_name,
):
    _run(monkeypatch, harness_name, REALLY_FAILED)

    assert ledger == [("i1", "failed")]


@pytest.mark.parametrize("tool,harness_name,signal", [
    ("test_run", "test_run", "tests_passed"),
    ("build_run", "build_run", "compiled"),
])
def test_a_verification_that_passed_is_unaffected(
    monkeypatch, ledger, tool, harness_name, signal,
):
    _run(monkeypatch, harness_name, {
        "ok": True, "returncode": 0, "timed_out": False, "stdout": "ok",
        "stderr": "", "elapsed_ms": 12, "command": ["pytest"],
    })

    assert ledger == [("i1", signal)]
