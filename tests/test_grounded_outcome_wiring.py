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


# --- the code runners -----------------------------------------------------
#
# `run_code`, `run_project` and `isolated_run` are verifiers too, and their
# result dict was discarded at the call site in exactly the same way. What is
# different is that `code_runner` overloads `error` for a genuine compilation
# failure, so these wrappers are wired to a predicate that reads `returncode`
# to tell a runner that could not start from code that ran and was rejected.

def _runner_result(**overrides):
    result = {
        "ok": False, "returncode": None, "stdout": "", "stderr": "",
        "language": "python", "cwd": "/w", "timeout": 10, "error": "",
    }
    result.update(overrides)
    return result


NO_TOOLCHAIN = _runner_result(
    language="cpp",
    error="C++ compiler not found (tried g++, clang++, cl, and Visual Studio vcvars64.bat)",
)
RUN_TIMED_OUT = _runner_result(error="timed out after 10s", stdout="partial")
# The trap: a real rustc verdict that also carries an `error` string.
RUST_COMPILE_FAILURE = _runner_result(
    language="rust", returncode=1, error="rust compilation failed",
    stderr="error[E0425]: cannot find value `x` in this scope",
)
RAN_AND_FAILED = _runner_result(returncode=1, stderr="ZeroDivisionError")


def _project_result(ok, *results):
    return {
        "ok": ok, "files": ["main.py"], "timeout": 60,
        "steps": [
            {"index": i, "cmd": ["python", "main.py"], "cwd": "/w", "result": r}
            for i, r in enumerate(results, start=1)
        ],
    }


@pytest.mark.parametrize("result", [NO_TOOLCHAIN, RUN_TIMED_OUT])
def test_run_code_writes_no_outcome_when_the_runner_could_not_run(
    monkeypatch, ledger, result,
):
    monkeypatch.setattr(server.code_runner, "run_code", lambda **_kwargs: result)

    server.run_code("print(1)")

    assert ledger == [], "run_code filed a verdict it never obtained"
    assert go.pending_count() == 1, "the generation is still awaiting real evidence"


@pytest.mark.parametrize("result", [RUST_COMPILE_FAILURE, RAN_AND_FAILED])
def test_run_code_still_writes_the_failure_when_the_code_really_ran(
    monkeypatch, ledger, result,
):
    monkeypatch.setattr(server.code_runner, "run_code", lambda **_kwargs: result)

    server.run_code("fn main() {}", language="rust")

    assert ledger == [("i1", "failed")]


def test_run_code_writes_no_outcome_when_it_raised(monkeypatch, ledger):
    """Empty code, an unsupported language, or a cwd outside the workspace all
    raise before anything is spawned."""
    def _raise(**_kwargs):
        raise ValueError("unsupported language 'brainfuck'")

    monkeypatch.setattr(server.code_runner, "run_code", _raise)

    out = server.run_code("x", language="brainfuck")

    assert out.startswith("ERROR: "), "the caller is still told it went wrong"
    assert ledger == []
    assert go.pending_count() == 1


def test_run_project_writes_no_outcome_when_a_step_could_not_run(
    monkeypatch, ledger,
):
    monkeypatch.setattr(
        server.code_runner, "run_project",
        lambda **_kwargs: _project_result(False, NO_TOOLCHAIN),
    )

    server.run_project('{"main.cpp": "int main(){}"}')

    assert ledger == []
    assert go.pending_count() == 1


def test_run_project_still_writes_the_failure_when_a_step_really_ran(
    monkeypatch, ledger,
):
    monkeypatch.setattr(
        server.code_runner, "run_project",
        lambda **_kwargs: _project_result(False, RAN_AND_FAILED),
    )

    server.run_project('{"main.py": "1/0"}')

    assert ledger == [("i1", "failed")]


def test_run_project_writes_no_outcome_when_it_raised(monkeypatch, ledger):
    def _raise(**_kwargs):
        raise ValueError("could not auto-detect how to run project")

    monkeypatch.setattr(server.code_runner, "run_project", _raise)

    out = server.run_project('{"notes.txt": "hi"}')

    assert out.startswith("ERROR: ")
    assert ledger == []
    assert go.pending_count() == 1


def _authorize_isolated(monkeypatch):
    monkeypatch.setattr(server, "_admin_account_from_token", lambda _token: {"role": "developer"})
    monkeypatch.setattr(server.admin_auth, "require", lambda _account, _role: (True, ""))


ISOLATED_ARGS = {
    "token": "developer-token",
    "acknowledge_isolation_limits": True,
}

NO_CONTAINER_RUNTIME = {
    "ok": False, "returncode": None, "stdout": "", "stderr": "",
    "error": ("isolated execution unavailable: explicitly enable a ready local "
              "Docker or Podman engine with SONDER_ISOLATED_RUNTIME"),
    "runtime": "", "project": "", "writable_workspace": False,
}
CONTAINER_EXITED_NONZERO = {
    "ok": False, "returncode": 2, "stdout": "", "stderr": "assertion failed",
    "error": "", "runtime": "docker", "project": "/p",
    "writable_workspace": False, "cleanup": "not-required",
}


def test_isolated_run_writes_no_outcome_when_no_engine_is_available(
    monkeypatch, ledger, tmp_path,
):
    _authorize_isolated(monkeypatch)
    monkeypatch.setattr(
        server.isolated_runner, "run_isolated",
        lambda **_kwargs: NO_CONTAINER_RUNTIME,
    )

    server.isolated_run("busybox", '["true"]', str(tmp_path), **ISOLATED_ARGS)

    assert ledger == [], "a missing container engine judged nothing"
    assert go.pending_count() == 1


def test_isolated_run_still_writes_the_failure_when_the_container_ran(
    monkeypatch, ledger, tmp_path,
):
    _authorize_isolated(monkeypatch)
    monkeypatch.setattr(
        server.isolated_runner, "run_isolated",
        lambda **_kwargs: CONTAINER_EXITED_NONZERO,
    )

    server.isolated_run("busybox", '["false"]', str(tmp_path), **ISOLATED_ARGS)

    assert ledger == [("i1", "failed")]


def test_isolated_run_writes_no_outcome_when_it_was_denied(
    monkeypatch, ledger, tmp_path,
):
    """A developer-authorization denial spawns nothing, so it is not a verdict
    on anyone's code -- it used to file `failed` (-1.0) all the same."""
    monkeypatch.setattr(server, "_admin_account_from_token", lambda _token: None)
    monkeypatch.setattr(server.admin_auth, "require", lambda _account, _role: (False, "no"))

    out = server.isolated_run("busybox", '["true"]', str(tmp_path), **ISOLATED_ARGS)

    assert out.startswith("ERROR: ")
    assert ledger == []
    assert go.pending_count() == 1


def test_isolated_run_writes_no_outcome_when_the_runner_rejected_the_request(
    monkeypatch, ledger, tmp_path,
):
    _authorize_isolated(monkeypatch)

    def _raise(**_kwargs):
        raise ValueError(
            "container image inspection failed; the image must already be installed"
        )

    monkeypatch.setattr(server.isolated_runner, "run_isolated", _raise)

    out = server.isolated_run("ghost:1", '["true"]', str(tmp_path), **ISOLATED_ARGS)

    assert out.startswith("ERROR: ")
    assert ledger == []
    assert go.pending_count() == 1
