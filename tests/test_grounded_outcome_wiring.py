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
def test_a_verifier_that_raised_writes_no_outcome(
    monkeypatch, ledger, tool, harness_name,
):
    """The wrapper's `except` branch is the one path where ok=False reaches the
    ledger carrying no evidence at all.

    `harness_tools._resolve_root` raises before anything is spawned when the
    root is not a directory, and every wrapper turns that into
    `_record_direct_tool(..., ok=False)`. Nothing ran, so there is nothing to
    file -- but this path bypassed the unmeasured state entirely and kept
    writing `failed` (-1.0) against the pending generation.
    """
    def _raise(**_kwargs):
        raise ValueError("not a directory: /nope")

    monkeypatch.setattr(server.harness_tools, harness_name, _raise)

    out = getattr(server, tool)()

    assert out.startswith("ERROR: "), "the caller is still told it went wrong"
    assert ledger == [], "%s filed a verdict for a run that never started" % tool
    assert go.pending_count() == 1, "the generation is still awaiting real evidence"


@pytest.mark.parametrize("tool,harness_name", VERIFIERS)
def test_a_verification_that_really_failed_still_writes_the_failure(
    monkeypatch, ledger, tool, harness_name,
):
    _run(monkeypatch, harness_name, REALLY_FAILED)

    assert ledger == [("i1", "failed")]


@pytest.mark.parametrize("tool,harness_name", VERIFIERS)
@pytest.mark.parametrize("exc", [
    ValueError(),          # str() -> ""
    ValueError(""),        # str() -> ""
    OSError(),             # str() -> ""
], ids=["no-args", "empty-message", "bare-oserror"])
def test_a_verifier_that_raised_without_a_message_writes_no_outcome(
    monkeypatch, ledger, tool, harness_name, exc,
):
    """An exception carrying no message is still a run that never started.

    Every wrapper forwards ``evidence={"error": str(exc)}``, and ``str()`` on
    an exception raised with no argument is the empty string. The predicate
    read the VALUE's truthiness rather than the key's presence, so
    ``{"error": ""}`` was indistinguishable from "no error was reported" --
    which closed the unmeasured state for every exception carrying a message
    and left it open for exactly those that do not. A guard that only holds
    for well-worded exceptions is not a guard.
    """
    def _raise(**_kwargs):
        raise exc

    monkeypatch.setattr(server.harness_tools, harness_name, _raise)

    out = getattr(server, tool)()

    assert out.startswith("ERROR"), "the caller is still told it went wrong"
    assert ledger == [], (
        "%s filed a verdict for a message-less exception" % tool
    )
    assert go.pending_count() == 1, "the generation is still awaiting real evidence"


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
