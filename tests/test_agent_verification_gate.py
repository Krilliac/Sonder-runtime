"""A completion claim must be backed by a verification, or say it is not.

``calibration.should_verify`` measures how often delegated work was actually
judged good, and returns True when the record is poor *or* unmeasured. Until
now nothing hung off that answer inside the agent loop: the loop's single
success exit (``finish_final``) handed the model's own "done" back to the
caller unchanged, so a system with a 53%-judged-good record reported success
in exactly the same words as one with a 98% record.

This module pins the decision that now hangs off the measurement:

* When the record demands verification and the run cited none, the end report
  carries a measured standing -- ``should_verify``'s own ``reason`` string,
  verbatim, which is a projection of counts and never generated prose.
* When the record is measurably good, nothing is added.
* "Cited a verification" means a *currently valid* one. The tempting signal is
  ``used_tool_names`` -- it sits two lines from the receipt -- but it is
  monotonic and can never un-verify: a passing ``test_run`` followed by a
  failing one, or followed by three ``file_write``s, still reads as verified
  there. That is "default to verified when detection fails", and it would fire
  on the majority of real runs. The gate therefore reuses the ``validation_ok``
  discipline: latest-wins, cleared by any mutation or execution.
* Coverage is keyed on ``root``. The four verifiers scope their run with
  ``root``; ``path`` merely narrows *which* checks run and is empty on a
  default invocation. Reading ``path`` here would answer the wrong question
  and answer it "" on nearly every real call.
"""
from __future__ import annotations

import os

import pytest

import autopilot_controller
import calibration
import server


# Population shapes, expressed as the outcome-signal counts calibration reads.
UNMEASURED_RECORD: dict = {}                                  # 0 judged outcomes
POOR_RECORD = {"accepted": 40, "rejected": 60}                # 40% of 100
GOOD_RECORD = {"accepted": 95, "rejected": 5}                 # 95% of 100


class _FakeConn:
    """Stand-in connection; ``calibration._counts`` is what reads it."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _record(monkeypatch, counts):
    """Give the loop a measured record without touching the live store."""
    conns = []

    def _open():
        conn = _FakeConn()
        conns.append(conn)
        return conn

    monkeypatch.setattr(server, "_open_db", _open)
    monkeypatch.setattr(calibration, "_counts", lambda _conn: dict(counts))
    return conns


def _expected_reason():
    """The exact wording ``should_verify`` projects from the seeded record.

    Reads through the same ``calibration._counts`` seam ``_record`` installed,
    so this is the real projection rather than a copy of it pasted into the
    test -- a pasted copy would keep passing if the loop stopped quoting
    should_verify and started composing its own sentence.
    """
    return calibration.should_verify(_FakeConn(), "caller")


def _run(monkeypatch, responses, observations=(), **kwargs):
    """Drive one full agent loop with a scripted model and scripted tools."""
    # A predictor trained by an earlier test may otherwise schedule an extra
    # speculative inspection and consume a scripted observation.
    monkeypatch.setenv("SONDER_SPECULATION", "0")
    replies = list(responses)
    obs = iter(list(observations))
    monkeypatch.setattr(
        server, "_make_generate",
        lambda *a, **k: (lambda prompt, history=None: replies.pop(0)),
    )
    monkeypatch.setattr(
        server, "_agent_dispatch_observed", lambda *a, **k: next(obs),
    )
    kwargs.setdefault("max_steps", max(len(responses), 1))
    return server._agent_impl("do the work", **kwargs)


FINAL = '{"final":"the work is complete"}'
# `auto_checklist` denies any mutation until the run has inspected something.
# project_detect is a _WORK_INSPECTION_TOOLS member that is deliberately NOT a
# _WORK_VALIDATION_TOOLS member, so it satisfies that requirement without
# quietly setting the validation state the test is about.
INSPECT = '{"tool":"project_detect","args":{"path":"."}}'
INSPECTED = '{"root":".","manifests":[{"path":"pyproject.toml"}],"errors":[]}'
PASSING_RUN = "test run (pytest)\n  ok: True\n  returncode: 0"
FAILING_RUN = "test run (pytest)\n  ok: False\n  returncode: 1"


# --- the standing itself --------------------------------------------------


@pytest.mark.parametrize("counts", [UNMEASURED_RECORD, POOR_RECORD])
def test_an_unbacked_completion_claim_is_reported_unverified(monkeypatch, counts):
    """Poor *and* unmeasured both demand a citation; neither is a pass."""
    _record(monkeypatch, counts)

    out = _run(monkeypatch, [FINAL])

    assert out.startswith(server._AGENT_UNVERIFIED_PREFIX)
    assert "the work is complete" in out


@pytest.mark.parametrize("counts", [UNMEASURED_RECORD, POOR_RECORD])
def test_the_standing_quotes_the_measured_reason_verbatim(monkeypatch, counts):
    """Measured, never generated: the words are a projection of the counts."""
    _record(monkeypatch, counts)
    verify, reason = _expected_reason()
    assert verify is True

    out = _run(monkeypatch, [FINAL])

    assert reason in out
    # The reason carries the counts, not a mood.
    assert ("judged outcomes" in reason) and any(ch.isdigit() for ch in reason)


def test_a_measurably_good_record_adds_nothing(monkeypatch):
    _record(monkeypatch, GOOD_RECORD)
    assert calibration.should_verify(_FakeConn(), "caller")[0] is False

    out = _run(monkeypatch, [FINAL])

    assert out == "the work is complete"
    assert server._AGENT_UNVERIFIED_PREFIX not in out


def test_a_cited_passing_verification_satisfies_a_poor_record(monkeypatch):
    _record(monkeypatch, POOR_RECORD)

    out = _run(
        monkeypatch,
        ['{"tool":"test_run","args":{"root":"."}}', FINAL],
        [PASSING_RUN],
    )

    assert out == "the work is complete"


# --- the trap: "verified" must be able to become "unverified" again -------


def test_a_verification_followed_by_a_mutation_is_not_verified(
    monkeypatch, tmp_path,
):
    """The whole point. ``used_tool_names`` still says test_run ran.

    Deriving the standing from that monotonic set would report this run as
    verified even though the tests predate every byte that is now on disk.
    """
    target = tmp_path / "src.py"
    _record(monkeypatch, POOR_RECORD)

    receipt = _run(
        monkeypatch,
        [
            '{"tool":"test_run","args":{"root":"%s"}}' % tmp_path.as_posix(),
            '{"tool":"file_write","args":{"path":"%s","content":"x","mode":"overwrite"}}'
            % target.as_posix(),
            FINAL,
        ],
        [PASSING_RUN, "wrote 1 byte"],
        return_host_receipt=True,
    )

    # The monotonic signal is still there and still says "a verifier ran".
    assert "test_run" in receipt.tools
    # The honest ledger says the verification no longer covers the tree.
    assert receipt.output.startswith(server._AGENT_UNVERIFIED_PREFIX)


def test_a_later_failing_verification_invalidates_an_earlier_pass(monkeypatch):
    """Latest-wins, exactly as the existing validation ledger does."""
    _record(monkeypatch, POOR_RECORD)

    out = _run(
        monkeypatch,
        [
            '{"tool":"test_run","args":{"root":"."}}',
            '{"tool":"test_run","args":{"root":".","pattern":"broader"}}',
            FINAL,
        ],
        [PASSING_RUN, FAILING_RUN],
    )

    assert out.startswith(server._AGENT_UNVERIFIED_PREFIX)


def test_a_failing_verification_is_not_a_citation(monkeypatch):
    _record(monkeypatch, POOR_RECORD)

    out = _run(
        monkeypatch,
        ['{"tool":"test_run","args":{"root":"."}}', FINAL],
        [FAILING_RUN],
    )

    assert out.startswith(server._AGENT_UNVERIFIED_PREFIX)


# --- the trap: coverage is keyed on `root`, not `path` --------------------


def test_coverage_is_keyed_on_root_not_the_empty_path_argument(
    monkeypatch, tmp_path,
):
    """A default ``test_run`` names its tree with ``root`` and no ``path``.

    Keying coverage on ``path`` -- the argument the older file-validator reads
    -- would see "" here and refuse to count a verification that genuinely ran
    over the changed tree.
    """
    source = tmp_path / "pkg"
    source.mkdir()
    target = source / "a.py"
    _record(monkeypatch, POOR_RECORD)

    out = _run(
        monkeypatch,
        [
            '{"tool":"file_write","args":{"path":"%s","content":"x","mode":"overwrite"}}'
            % target.as_posix(),
            '{"tool":"test_run","args":{"root":"%s"}}' % tmp_path.as_posix(),
            FINAL,
        ],
        ["wrote 1 byte", PASSING_RUN],
    )

    assert out == "the work is complete"


def test_a_verification_run_somewhere_else_does_not_cover_the_change(
    monkeypatch, tmp_path,
):
    changed = tmp_path / "worked-on"
    elsewhere = tmp_path / "elsewhere"
    changed.mkdir()
    elsewhere.mkdir()
    _record(monkeypatch, POOR_RECORD)

    out = _run(
        monkeypatch,
        [
            '{"tool":"file_write","args":{"path":"%s","content":"x","mode":"overwrite"}}'
            % (changed / "a.py").as_posix(),
            '{"tool":"test_run","args":{"root":"%s"}}' % elsewhere.as_posix(),
            FINAL,
        ],
        ["wrote 1 byte", PASSING_RUN],
    )

    assert out.startswith(server._AGENT_UNVERIFIED_PREFIX)


def test_every_verifier_task_2_made_reachable_can_satisfy_the_gate(
    monkeypatch, tmp_path,
):
    """test_run/build_run/lint_run/typecheck_run were uncallable before Task 2.

    This drives the loop through the branch that would actually punish a
    mis-classification: ``auto_checklist`` on, with a real mutation, so
    ``server.py``'s ``VALIDATION_FAILED`` branch is *reachable*. If any of these
    four names were in ``_WORK_VALIDATION_TOOLS``, ``_agent_validation_covers``
    would fall through to False and running the verifier would be the thing that
    stamped the run failed. Without the mutation the branch is unreachable and
    the test proves nothing about the trap.
    """
    assert server._AGENT_VERIFICATION_TOOLS == frozenset({
        "test_run", "build_run", "lint_run", "typecheck_run",
    })
    for name in sorted(server._AGENT_VERIFICATION_TOOLS):
        _record(monkeypatch, POOR_RECORD)
        receipt = _run(
            monkeypatch,
            [
                INSPECT,
                '{"tool":"file_write","args":{"path":"%s","content":"x",'
                '"mode":"overwrite"}}' % (tmp_path / "a.py").as_posix(),
                '{"tool":"%s","args":{"root":"%s"}}' % (name, tmp_path.as_posix()),
                FINAL,
            ],
            [INSPECTED, "wrote 1 byte", "%s\n  ok: True\n  returncode: 0" % name],
            auto_checklist=True,
            return_host_receipt=True,
        )
        # Without this the test can pass vacuously: `auto_checklist` denies a
        # mutation until something has been inspected, and a denied file_write
        # leaves `mutated` False, which makes the VALIDATION_FAILED branch
        # unreachable and the assertions below true under any implementation.
        assert receipt.mutation_observed is True, name
        assert not receipt.output.startswith("VALIDATION_FAILED:"), name
        assert receipt.output == "the work is complete", name


def test_the_verifiers_are_not_added_to_the_file_validation_set():
    """Adding them to ``_WORK_VALIDATION_TOOLS`` would make tests *fail* runs.

    ``_agent_validation_covers`` falls through to False for any tool it has no
    branch for, so a name added there marks ``validation_ok=False`` and the run
    picks up VALIDATION_FAILED -- running the tests would be what broke it.
    """
    assert not (
        server._AGENT_VERIFICATION_TOOLS & server._WORK_VALIDATION_TOOLS
    )


# --- the standing must never displace an existing failure marker ---------
#
# `autopilot_controller._task_passed` matches `text.startswith(FAILURE_PREFIXES)`
# and `_agent_observation_ok` reads the first line. Stacking a second prefix in
# front of VALIDATION_FAILED moves it off position 0 and both consumers go
# blind -- a mechanism built to make claims more honest would have made a
# failure gate stop failing. The two statements are composed into one leading
# block instead, with the failure marker still leading it.


def _failed_validation_run(monkeypatch, tmp_path, counts=UNMEASURED_RECORD):
    """A mutating run, checklist on, that never validates what it changed."""
    _record(monkeypatch, counts)
    return _run(
        monkeypatch,
        [
            INSPECT,
            '{"tool":"file_write","args":{"path":"%s","content":"x",'
            '"mode":"overwrite"}}' % (tmp_path / "a.py").as_posix(),
            FINAL,
        ],
        [INSPECTED, "wrote 1 byte"],
        auto_checklist=True,
        return_host_receipt=True,
    )


def test_a_failed_validation_still_leads_the_report(monkeypatch, tmp_path):
    receipt = _failed_validation_run(monkeypatch, tmp_path)

    assert receipt.output.startswith("VALIDATION_FAILED:")
    # ...and the measured standing is still stated, in the same block.
    assert "judged outcomes" in receipt.output.split("\n\n")[0]


def test_the_failed_validation_first_line_is_byte_identical(monkeypatch, tmp_path):
    """`_agent_observation_ok` and `set_result_summary` both read line one.

    Spelled out as a literal rather than compared against
    ``server._AGENT_VALIDATION_FAILED_LINE`` on purpose: this pins the text
    consumers saw *before* this task existed, so editing the constant cannot
    quietly take the test with it.
    """
    receipt = _failed_validation_run(monkeypatch, tmp_path)

    assert receipt.output.splitlines()[0] == (
        "VALIDATION_FAILED: workspace changes were not successfully validated."
    )


def test_both_statements_read_as_written_english(monkeypatch, tmp_path):
    """The shared noun phrase must slot into both forms grammatically.

    ``_AGENT_VERIFIERS_PHRASE`` starts with an article, so a composed clause
    reading "cited no <phrase>" produced "no a passing verification". A shared
    constant stops the two forms describing different things; it does not stop
    one of them reading badly.
    """
    composed = _failed_validation_run(monkeypatch, tmp_path).output
    _record(monkeypatch, UNMEASURED_RECORD)
    standalone = _run(monkeypatch, [FINAL])

    for text in (composed, standalone):
        assert server._AGENT_VERIFIERS_PHRASE in text
        assert "no a passing" not in text
        assert "without a passing verification" in text


def test_the_autopilot_gate_still_rejects_a_failed_validation(
    monkeypatch, tmp_path,
):
    """Drive the real gate, not a string assertion standing in for it."""
    receipt = _failed_validation_run(monkeypatch, tmp_path)

    ok, why = autopilot_controller._task_passed(receipt, {"kind": "implement"})

    assert ok is False
    assert why.startswith("VALIDATION_FAILED:")


def test_a_nested_failed_validation_still_reads_as_a_bad_observation(
    monkeypatch, tmp_path,
):
    """A sub-agent's end report becomes its parent's observation text."""
    receipt = _failed_validation_run(monkeypatch, tmp_path)

    assert server._agent_observation_ok(receipt.output) is False


def test_the_activity_summary_still_names_the_failure(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(
        server.activity_tracker, "set_result_summary", captured.append,
    )

    _failed_validation_run(monkeypatch, tmp_path)

    assert captured == [
        "VALIDATION_FAILED: workspace changes were not successfully validated."
    ]


def test_a_covering_verification_satisfies_the_validation_gate_too(
    monkeypatch, tmp_path,
):
    """Resolves the contradiction between the two gates.

    ``validation_ok`` and ``verification_ok`` answer the same question -- was
    the change actually checked -- over disjoint tool sets. Before Task 2 the
    four verifiers were undispatchable, so the only way to validate a mutation
    was to shell out through ``workspace_run``; leaving them out of the
    validation gate now fails a run precisely *for using the purpose-built
    tool*, while reporting the verification satisfied in the same breath.
    """
    _record(monkeypatch, POOR_RECORD)

    receipt = _run(
        monkeypatch,
        [
            INSPECT,
            '{"tool":"file_write","args":{"path":"%s","content":"x",'
            '"mode":"overwrite"}}' % (tmp_path / "a.py").as_posix(),
            '{"tool":"test_run","args":{"root":"%s"}}' % tmp_path.as_posix(),
            FINAL,
        ],
        [INSPECTED, "wrote 1 byte", PASSING_RUN],
        auto_checklist=True,
        return_host_receipt=True,
    )

    assert receipt.output == "the work is complete"
    assert receipt.mutation_observed is True
    # The receipt must agree with the report it accompanies, or a "validate"
    # task is rejected with "ran no host-observed validator" while the report
    # says nothing failed.
    assert receipt.validation_attempted is True
    assert receipt.validation_passed is True
    ok, why = autopilot_controller._task_passed(receipt, {"kind": "validate"})
    assert ok, why


def test_a_failing_verification_does_not_satisfy_the_validation_gate(
    monkeypatch, tmp_path,
):
    """The satisfying condition is a *passing*, covering verifier -- nothing less."""
    _record(monkeypatch, POOR_RECORD)

    receipt = _run(
        monkeypatch,
        [
            INSPECT,
            '{"tool":"file_write","args":{"path":"%s","content":"x",'
            '"mode":"overwrite"}}' % (tmp_path / "a.py").as_posix(),
            '{"tool":"test_run","args":{"root":"%s"}}' % tmp_path.as_posix(),
            FINAL,
        ],
        [INSPECTED, "wrote 1 byte", FAILING_RUN],
        auto_checklist=True,
        return_host_receipt=True,
    )

    assert receipt.output.startswith("VALIDATION_FAILED:")
    assert receipt.validation_passed is False
    ok, _why = autopilot_controller._task_passed(receipt, {"kind": "implement"})
    assert ok is False


# --- the standing is a statement, not a failure ---------------------------


def test_the_standing_is_not_a_failure_prefix():
    """An unverified run is not a failed run; it is an honest one."""
    assert server._AGENT_UNVERIFIED_PREFIX not in autopilot_controller.FAILURE_PREFIXES
    assert not server._AGENT_UNVERIFIED_PREFIX.startswith(
        autopilot_controller.FAILURE_PREFIXES
    )


def test_an_unverified_run_still_passes_the_workbench_acceptance_check(
    monkeypatch, tmp_path,
):
    _record(monkeypatch, UNMEASURED_RECORD)

    receipt = _run(
        monkeypatch,
        ['{"tool":"file_read","args":{"path":"%s"}}' % (tmp_path / "x").as_posix(), FINAL],
        ["contents"],
        return_host_receipt=True,
    )

    assert receipt.output.startswith(server._AGENT_UNVERIFIED_PREFIX)
    ok, why = autopilot_controller._task_passed(receipt, {"kind": "inspect"})
    assert ok, why


def test_the_activity_summary_still_names_the_work_not_the_standing(monkeypatch):
    """The standing must not evict the model's own first line from the feed."""
    _record(monkeypatch, UNMEASURED_RECORD)
    captured = []
    monkeypatch.setattr(
        server.activity_tracker, "set_result_summary", captured.append,
    )

    out = _run(monkeypatch, [FINAL])

    assert out.startswith(server._AGENT_UNVERIFIED_PREFIX)
    assert captured == ["the work is complete"]


# --- ignorance fails closed, everywhere ----------------------------------


@pytest.mark.parametrize(
    "lane", [
        {},                                    # workspace agent
        {"auto_checklist": True},              # workbench / autopilot
        {"read_only": True},                   # repository worker
        {"allow_web": True, "read_only": True},  # chat / web research
    ],
)
def test_the_gate_covers_every_lane_not_just_the_mutating_one(monkeypatch, lane):
    """Gating on ``mutated``/``auto_checklist`` would exempt 4 of the 5 lanes."""
    _record(monkeypatch, UNMEASURED_RECORD)

    out = _run(monkeypatch, [FINAL], **lane)

    assert out.startswith(server._AGENT_UNVERIFIED_PREFIX)


def test_an_unreadable_record_demands_verification_rather_than_assuming(
    monkeypatch,
):
    """Below MIN_SAMPLE and below *any* sample both fail closed."""
    def _boom():
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(server, "_open_db", _boom)

    out = _run(monkeypatch, [FINAL])

    assert out.startswith(server._AGENT_UNVERIFIED_PREFIX)


def test_the_connection_is_closed_even_though_the_loop_continues(monkeypatch):
    conns = _record(monkeypatch, UNMEASURED_RECORD)

    _run(monkeypatch, [FINAL])

    assert conns and all(conn.closed for conn in conns)
