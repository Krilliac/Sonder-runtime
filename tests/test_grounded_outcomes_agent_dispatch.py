"""grounded_outcomes must also see work done through the agent loop.

Before this, ``grounded_outcomes`` was wired only into ``_record_direct_tool``,
which fires for direct MCP tool calls. The agent loop, ``workbench_agent``, and
autopilot all dispatch tools through ``_agent_dispatch_observed`` instead, and
that path fed nothing -- so a generation-then-verification sequence run
entirely inside one agent step never produced an outcome row.

These tests exercise the real ``_agent_dispatch_observed`` (not a stand-in),
stubbing only the two things that would otherwise touch the network/DB: the
underlying ``_agent_dispatch`` call and the outcome-row writer.
"""
import threading

import pytest

import grounded_outcomes as go
import server


@pytest.fixture(autouse=True)
def _clean():
    go.reset()
    yield
    go.reset()


def _sink(monkeypatch):
    """Capture what would have been written to the outcome store."""
    written = []
    monkeypatch.setattr(
        server, "_record_outcome_signal",
        lambda ident, signal: written.append((ident, signal)),
    )
    return written


# What a verifier that RAN and rejected the work actually renders. Measured
# from code_runner.format_result() on `raise SystemExit(3)`. The previous
# fixture used "ERROR: ..." for this, and no verifier in VERIFIERS ever
# produces that for a failing run: server.run_code returns "ERROR: %s" only
# for a ValueError out of code_runner (unsupported language, bad args), i.e.
# for a tool that never ran the code at all, and the four harness verifiers
# return it only from their `except` branch. So the old fixture pinned "a
# verification that could not run files failed" while calling it a normal
# failure -- the conflation task #56 removes. Every assertion below is kept;
# only the stand-in changed, to one a real verifier can actually emit.
REJECTED = "\n".join([
    "status: failed",
    "language: python",
    "cwd: /tmp/x",
    "timeout: 10s",
    "returncode: 3",
    "",
    "(no output)",
])


def _stub_dispatch(monkeypatch, responses):
    """Replace _agent_dispatch with a canned tool_name -> observation map."""
    def fake(tool_name, args, **kwargs):
        return responses[tool_name]
    monkeypatch.setattr(server, "_agent_dispatch", fake)


# --- the happy path: generate, then verify -------------------------------


def test_agent_loop_generation_then_failed_verification_files_exactly_one_failed(monkeypatch):
    written = _sink(monkeypatch)
    _stub_dispatch(monkeypatch, {
        "offload": "here is the code\n\n[interaction_id: run-1]",
        "run_code": REJECTED,
    })

    server._agent_dispatch_observed("offload", {"prompt": "write a function"})
    server._agent_dispatch_observed("run_code", {"code": "print(x"})

    assert written == [("run-1", "failed")]


def test_agent_loop_generation_then_passing_verification_files_the_positive_signal(monkeypatch):
    written = _sink(monkeypatch)
    _stub_dispatch(monkeypatch, {
        "offload": "here is the code\n\n[interaction_id: run-2]",
        "run_code": "ok: 3 lines executed",
    })

    server._agent_dispatch_observed("offload", {"prompt": "write a function"})
    server._agent_dispatch_observed("run_code", {"code": "print(1)"})

    assert written == [("run-2", "compiled")]


# --- must not double-file --------------------------------------------------


def test_a_rerun_of_the_same_verifier_does_not_file_twice(monkeypatch):
    """One verification kind judges a generation only once (grounded_outcomes'
    own rule), so retrying the same failing call must not double-count it."""
    written = _sink(monkeypatch)
    _stub_dispatch(monkeypatch, {
        "offload": "code\n\n[interaction_id: run-3]",
        "run_code": REJECTED,
    })

    server._agent_dispatch_observed("offload", {"prompt": "x"})
    server._agent_dispatch_observed("run_code", {"code": "bad"})
    server._agent_dispatch_observed("run_code", {"code": "bad"})   # retried

    assert written == [("run-3", "failed")]


def test_a_tool_that_files_its_own_direct_outcome_is_not_fed_twice_via_the_loop(monkeypatch):
    """file_write is both a GENERATOR and a tool that calls _record_direct_tool
    at its own end (the direct-call path). When dispatched through the agent
    loop it runs inside tool_dispatch_context(), so its own internal call must
    see inside_tool_call() == True and skip -- leaving _agent_dispatch_observed's
    own feed as the only one that fires. This test reproduces exactly that
    shape without needing the real filesystem: the stand-in dispatch function
    calls _record_direct_tool itself, mirroring what file_write really does.
    """
    written = _sink(monkeypatch)

    def fake_dispatch(tool_name, args, **kwargs):
        observation = "wrote file\n\n[interaction_id: run-4]"
        # What the real tool body does just before returning: record via the
        # direct-call path. This executes while _agent_dispatch_observed's
        # tool_dispatch_context() is active.
        assert server.activity_tracker.inside_tool_call() is True
        server._record_direct_tool("file_write", args, ok=True, output=observation)
        return observation

    monkeypatch.setattr(server, "_agent_dispatch", fake_dispatch)

    server._agent_dispatch_observed("file_write", {"path": "a.py"})
    assert go.pending_count() == 1, "the generation must be noted exactly once"

    _stub_dispatch(monkeypatch, {"run_code": "ok"})
    server._agent_dispatch_observed("run_code", {"code": "1"})

    assert written == [("run-4", "compiled")]


# --- no verification means no outcome --------------------------------------


def test_a_run_with_no_verification_files_nothing(monkeypatch):
    written = _sink(monkeypatch)
    _stub_dispatch(monkeypatch, {
        "offload": "here is the plan\n\n[interaction_id: run-5]",
    })

    server._agent_dispatch_observed("offload", {"prompt": "plan only"})

    assert written == []
    assert go.pending_count() == 1, "the generation is still awaiting evidence"


def test_a_read_only_inspection_tool_is_neither_generator_nor_verifier(monkeypatch):
    written = _sink(monkeypatch)
    _stub_dispatch(monkeypatch, {"file_read": "contents of the file"})

    server._agent_dispatch_observed("file_read", {"path": "a.py"}, read_only=True)

    assert written == []
    assert go.pending_count() == 0


# --- project scoping travels through the explicit `project` param ---------


def test_the_dispatch_projects_scope_is_used_for_attribution_not_the_raw_args(monkeypatch):
    """_agent_dispatch_observed knows the run's project directly (the `project`
    kwarg); attribution must key off that, not off an args dict that may not
    even carry a "project"/"root" field for this tool (e.g. run_code uses
    neither)."""
    written = _sink(monkeypatch)
    _stub_dispatch(monkeypatch, {
        "offload": "code\n\n[interaction_id: run-6]",
        "run_code": REJECTED,
    })

    server._agent_dispatch_observed(
        "offload", {"prompt": "x"}, project="D:\\proj-a",
    )
    # A verification for a DIFFERENT project must not claim this generation.
    server._agent_dispatch_observed(
        "run_code", {"code": "y"}, project="D:\\proj-b",
    )
    assert written == [], "a different named project must not match"

    server._agent_dispatch_observed(
        "run_code", {"code": "y"}, project="D:\\proj-a",
    )
    assert written == [("run-6", "failed")]


# --- a dispatcher crash is plumbing failure, not a verdict ------------------


def test_a_dispatcher_crash_on_a_verifier_files_nothing(monkeypatch):
    """_agent_dispatch raising -- as opposed to returning its normal
    "ERROR: ..." string -- means the dispatch plumbing broke (a bad arg, an
    internal KeyError, an IO fault before the tool's own try/except even
    engages), not that the generated work failed to build or pass. Filing a
    verdict from that would poison the very population grounded_outcomes
    exists to clean up, exactly the kind of wrong link the module's own
    docstring calls worse than recording nothing.
    """
    written = _sink(monkeypatch)
    go.note_generation("run-x", "sonder")

    def crash(tool_name, args, **kwargs):
        raise KeyError("dispatch blew up")

    monkeypatch.setattr(server, "_agent_dispatch", crash)

    with pytest.raises(KeyError):
        server._agent_dispatch_observed("run_code", {"code": "1"})

    assert written == [], "a plumbing crash must never be filed as a verdict"
    assert go.pending_count() == 1, "the generation is still awaiting real evidence"


def test_a_dispatcher_crash_on_a_generator_notes_nothing(monkeypatch):
    def crash(tool_name, args, **kwargs):
        raise RuntimeError("dispatch blew up")

    monkeypatch.setattr(server, "_agent_dispatch", crash)

    with pytest.raises(RuntimeError):
        server._agent_dispatch_observed("offload", {"prompt": "x"})

    assert go.pending_count() == 0, "a crash produced no real output to mine an id from"


def test_a_normal_tool_failure_still_files_as_before(monkeypatch):
    """The crash guard must not swallow the ordinary case: a tool that
    dispatches cleanly and rejects the work is a real verdict and must
    still be filed.

    The stand-in used to be an "ERROR: ..." string. Measured, that is not
    what a rejecting verifier renders -- it is what a verifier that never
    ran renders -- so the old form pinned the misattribution instead of the
    intent. See ``test_an_error_observation_files_nothing``.
    """
    written = _sink(monkeypatch)
    go.note_generation("run-y", "sonder")
    _stub_dispatch(monkeypatch, {"run_code": REJECTED})

    server._agent_dispatch_observed("run_code", {"code": "bad"})

    assert written == [("run-y", "failed")]


# --- concurrent runs must not cross-attribute -------------------------------


def test_two_concurrent_runs_do_not_cross_attribute(monkeypatch):
    """Reproduces autopilot's real shape: one activity_tracker response span
    per run, on its own thread (_autopilot_thread_main wraps
    _execute_autopilot in exactly this). Two runs sharing a project -- or, as
    here, both leaving it blank -- must not let one run's verification claim
    the OTHER run's generation.

    Before run/span scoping, _candidate always matched the newest pending
    generation regardless of which run produced it, so interleaving run B's
    (newer) generation ahead of run A's own verification reproduces exactly
    that cross-run contamination: run A's failing run_code would have been
    filed against run B's still-unverified generation instead of its own.
    """
    written = _sink(monkeypatch)

    def fake_dispatch(tool_name, args, **kwargs):
        if tool_name == "offload":
            return "code\n\n[interaction_id: %s]" % args["tag"]
        if tool_name == "run_code":
            return REJECTED
        raise AssertionError("unexpected tool %r" % tool_name)

    monkeypatch.setattr(server, "_agent_dispatch", fake_dispatch)

    run_b_done = threading.Event()

    def run_b():
        with server.activity_tracker.response_span("run-b", "", surface="test"):
            server._agent_dispatch_observed("offload", {"prompt": "x", "tag": "gen-b"})
        run_b_done.set()

    def run_a():
        with server.activity_tracker.response_span("run-a", "", surface="test"):
            server._agent_dispatch_observed("offload", {"prompt": "x", "tag": "gen-a"})
            thread_b = threading.Thread(target=run_b)
            thread_b.start()
            assert run_b_done.wait(timeout=5), "run B never finished noting its generation"
            # Two generations are now pending: gen-a (run A's own, older) and
            # gen-b (run B's, newer -- and, absent run scoping, the one
            # _candidate would hand to any project-unscoped verifier next).
            server._agent_dispatch_observed("run_code", {"code": "bad"})
            thread_b.join(timeout=5)

    run_a()

    assert written == [("gen-a", "failed")], (
        "run A's own verification must judge run A's own generation, "
        "not run B's newer, unrelated one"
    )


def test_an_error_observation_files_nothing(monkeypatch):
    """The other half of the same distinction, and the task #56 fix.

    ``server.run_code`` returns ``"ERROR: %s" % e`` only when ``code_runner``
    raised ``ValueError`` -- an unsupported language, a bad argument -- so the
    snippet was never executed and nothing was measured. The four harness
    verifiers return it only from their ``except`` branch, for the same reason.
    Filing ``failed`` (-1.0) from that scores work nothing examined, and worse,
    consumes the generation's one chance at a real verdict for that kind.
    """
    written = _sink(monkeypatch)
    go.note_generation("run-z", "sonder")
    _stub_dispatch(monkeypatch, {"run_code": "ERROR: unsupported language 'q'"})

    server._agent_dispatch_observed("run_code", {"code": "bad"})

    assert written == []
    assert go.pending_count() == 1, "the generation still awaits real evidence"


def test_a_failing_suite_is_not_filed_as_a_pass(monkeypatch):
    """The inverted half, found while fixing #56 and strictly worse than it.

    ``_agent_dispatch_observed`` computes ``ok = not observation.startswith(
    "ERROR:")``, which is a statement about the dispatcher. Measured, a pytest
    suite with a failing test renders ``"test run (pytest)\n  ok: False\n
    returncode: 1"`` -- no ``ERROR:`` prefix -- so it arrived as ``ok=True``
    and was filed ``tests_passed`` at +1.0. A manufactured success in a store
    whose caller-judged population sits near 52% is indistinguishable from
    real progress.
    """
    written = _sink(monkeypatch)
    go.note_generation("run-w", "sonder")
    _stub_dispatch(monkeypatch, {
        "test_run": "\n".join([
            "test run (pytest)",
            "  command: []",
            "  cwd: /tmp/x",
            "  ok: False",
            "  returncode: 1",
            "  timed_out: False",
            "  elapsed_ms: 12",
            "stdout:",
            "1 failed, 2 passed",
        ]),
    })

    server._agent_dispatch_observed("test_run", {"root": "/tmp/x"})

    assert written == [("run-w", "failed")]
