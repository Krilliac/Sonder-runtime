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
* Coverage is keyed on ``root`` NARROWED BY ``path``. This file used to say
  ``path`` "merely narrows *which* checks run" and that reading it "would
  answer the wrong question". The first half was refuted by ``harness_tools``,
  which appends ``path`` straight to the child argv -- so ``path`` is what the
  check exercises. The second half was right, and is why the narrowing is
  conditional on ``path`` being non-empty. A verifier narrowed to ``tests/``
  no longer counts as covering a change to ``payments.py``.
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

    # Both openers: the standing reads through the read-only path, and this
    # helper must not be the thing that decides which one the loop uses.
    monkeypatch.setattr(server, "_open_db", _open)
    monkeypatch.setattr(server, "_open_db_readonly", _open)
    # Signature-agnostic: `_counts` gained a provenance filter with #62 and
    # no assertion here concerns its parameters. A double that pins an
    # argument list it never checks fails on changes it does not test.
    monkeypatch.setattr(calibration, "_counts", lambda *a, **k: dict(counts))
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


# --- a run that changed nothing still has to be covered ------------------
#
# `all([])` is True, so with no mutation records the coverage check reported
# "covered" for a verifier rooted anywhere -- a check answering yes because it
# had nothing to check. That matters now that a passing verifier also sets
# `validation_attempted`/`validation_passed`: `_task_passed` and
# `_completion_gate` accept that receipt for a whole `validate` task.
#
# The sibling `_agent_validation_covers` already decides its no-records case
# explicitly rather than falling through an empty `all()`; this follows that
# shape. With nothing changed on disk, the work the run was confined to *is*
# the project scope, so the verifier has to cover that.


@pytest.mark.parametrize(
    "root_of, expected",
    [
        ("project", True),     # rooted exactly at the declared scope
        ("subdir", False),     # a slice of the project is not the project
        ("elsewhere", False),  # outside it entirely
    ],
)
def test_a_non_mutating_verifier_must_cover_the_declared_scope(
    tmp_path, root_of, expected,
):
    """Unit-level, because the loop cannot reach every case.

    A project-bound run has the host reject an out-of-scope ``root`` before
    dispatch, so an end-to-end test of the "elsewhere" case would pass because
    the call was *blocked*, not because coverage said no -- vacuously green,
    proving nothing about this function. The subdirectory case below is the one
    that is genuinely reachable through the loop, and is tested there too.

    Every one of these returned True before the fix, via ``all([])``.
    """
    project = tmp_path / "project"
    subdir = project / "pkg"
    elsewhere = tmp_path / "elsewhere"
    for directory in (project, subdir, elsewhere):
        directory.mkdir(parents=True, exist_ok=True)
    roots = {"project": project, "subdir": subdir, "elsewhere": elsewhere}

    covered = server._agent_verification_covers(
        "test_run", {"root": str(roots[root_of])}, [],
        project_scope=str(project),
    )

    assert covered is expected


def test_an_unscoped_non_mutating_run_is_decided_not_skipped(tmp_path):
    """The one case that stays True, stated as a decision rather than a default.

    With no declared project there is no boundary to violate: ``root`` defaults
    to the server CWD, which is the run's implicit scope. That default is the
    separately-tracked fail-closed item, not something this check invents.
    """
    covered = server._agent_verification_covers(
        "test_run", {"root": str(tmp_path)}, [], project_scope="",
    )

    assert covered is True


def test_a_verifier_on_one_subdirectory_does_not_validate_the_project(
    monkeypatch, tmp_path,
):
    """The reachable end-to-end case, and the one `_completion_gate` accepts."""
    project = tmp_path / "project"
    (project / "pkg").mkdir(parents=True)
    _record(monkeypatch, POOR_RECORD)

    receipt = _run(
        monkeypatch,
        [
            '{"tool":"test_run","args":{"root":"%s"}}'
            % (project / "pkg").as_posix(),
            FINAL,
        ],
        [PASSING_RUN],
        project=str(project),
        return_host_receipt=True,
    )

    # Guards against a vacuous pass: the verifier really ran and really
    # changed nothing, so the no-mutation branch is the one under test.
    assert "test_run" in receipt.tools
    assert receipt.mutation_observed is False

    assert receipt.output.startswith(server._AGENT_UNVERIFIED_PREFIX)
    assert receipt.validation_passed is False
    ok, why = autopilot_controller._task_passed(receipt, {"kind": "validate"})
    assert ok is False
    assert "coverage" in why


def test_a_verifier_rooted_at_the_project_does_validate_it(
    monkeypatch, tmp_path,
):
    """The fix must not simply refuse everything that changed nothing."""
    project = tmp_path / "project"
    project.mkdir()
    _record(monkeypatch, POOR_RECORD)

    receipt = _run(
        monkeypatch,
        ['{"tool":"test_run","args":{"root":"."}}', FINAL],
        [PASSING_RUN],
        project=str(project),
        return_host_receipt=True,
    )

    assert "test_run" in receipt.tools
    assert receipt.mutation_observed is False
    assert receipt.output == "the work is complete"
    assert receipt.validation_passed is True
    ok, why = autopilot_controller._task_passed(receipt, {"kind": "validate"})
    assert ok, why


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


# Lanes, expressed as the two gates that actually decide whether a verifier is
# callable: the read-only policy (which admits only REPOSITORY_READ_ONLY_TOOLS)
# and the lane's own tool_allowlist. The previous version of this table listed
# lane *flags* -- {"read_only": True} with no allowlist -- which is not what any
# real caller passes, so it exercised none of the restrictions it named.
LANES_THAT_CAN_VERIFY = {
    "workspace agent (no allowlist)": {},
    "workbench / autopilot workspace": {"auto_checklist": True},
    "autopilot workspace allowlist": {
        "tool_allowlist": tuple(sorted(server._AUTOPILOT_WORKSPACE_TOOLS)),
    },
}

# Lanes where no member of _AGENT_VERIFICATION_TOOLS is reachable at all.
LANES_THAT_CANNOT_VERIFY = {
    "repository worker": {
        "read_only": True,
        "tool_allowlist": tuple(sorted(server.REPOSITORY_READ_ONLY_TOOLS)),
    },
    "autopilot observe": {
        "read_only": True,
        "tool_allowlist": tuple(sorted(server._AUTOPILOT_OBSERVE_TOOLS)),
    },
    "chat / web research": {
        "allow_web": True,
        "tool_allowlist": (
            "web_search", "web_fetch", "weather_lookup",
            "approximate_location_lookup",
        ),
    },
    "selfmod editor": {
        "tool_allowlist": (
            "workspace_inventory", "directory_tree", "text_search", "file_read",
            "file_read_range", "file_write", "file_edit", "file_delete",
        ),
    },
}


def test_the_lane_tables_match_what_the_gates_actually_admit():
    """Derived, not asserted: the tables above are checked against the sets."""
    verifiers = server._AGENT_VERIFICATION_TOOLS
    assert verifiers, "the verifier set must not be empty"
    for name, lane in LANES_THAT_CANNOT_VERIFY.items():
        allowed = set(lane.get("tool_allowlist") or ())
        assert not (verifiers & allowed), "%s can reach %s" % (
            name, sorted(verifiers & allowed),
        )
    # read_only admits only REPOSITORY_READ_ONLY_TOOLS, which holds no verifier.
    assert not (verifiers & server.REPOSITORY_READ_ONLY_TOOLS)
    assert verifiers & server._AUTOPILOT_WORKSPACE_TOOLS


@pytest.mark.parametrize("lane", sorted(LANES_THAT_CAN_VERIFY))
def test_every_lane_that_can_verify_is_gated(monkeypatch, lane):
    """Gating on ``mutated``/``auto_checklist`` would exempt most of these."""
    _record(monkeypatch, UNMEASURED_RECORD)

    out = _run(monkeypatch, [FINAL], **LANES_THAT_CAN_VERIFY[lane])

    assert out.startswith(server._AGENT_UNVERIFIED_PREFIX)


@pytest.mark.parametrize("lane", sorted(LANES_THAT_CAN_VERIFY))
def test_every_lane_that_can_verify_can_also_clear_it(monkeypatch, lane):
    """The assertion the old table never made.

    Pinning only that the standing FIRES documents a banner just as happily as
    it documents a gate. A gate is a thing with an OFF state, so each lane that
    is told to cite a verifier must be able to cite one and come out clean.
    """
    _record(monkeypatch, POOR_RECORD)

    out = _run(
        monkeypatch,
        ['{"tool":"test_run","args":{"root":"."}}', FINAL],
        [PASSING_RUN],
        **LANES_THAT_CAN_VERIFY[lane],
    )

    assert server._AGENT_UNVERIFIED_PREFIX not in out


@pytest.mark.parametrize("lane", sorted(LANES_THAT_CANNOT_VERIFY))
def test_a_lane_with_no_reachable_verifier_is_not_told_to_run_one(
    monkeypatch, lane,
):
    """A demand nothing in the lane can satisfy is a banner, not a gate.

    These lanes cannot call test_run/build_run/lint_run/typecheck_run at all,
    so leading every answer -- including every weather question -- with
    "claimed completion without a passing verification (test_run, build_run,
    lint_run or typecheck_run)" names tools the lane is forbidden from using
    and can never be acted on. The measured record is unchanged and still
    reaches the caller through the end-report standing line; what goes away is
    an instruction that has no OFF state.
    """
    _record(monkeypatch, UNMEASURED_RECORD)

    out = _run(monkeypatch, [FINAL], **LANES_THAT_CANNOT_VERIFY[lane])

    assert out == "the work is complete"
    assert server._AGENT_UNVERIFIED_PREFIX not in out


def test_scoping_is_read_from_the_gates_not_from_a_list_of_lane_names():
    """A lane added later is classified by what it can do, not by memory."""
    assert server._agent_verifier_reachable(read_only=False, allowed_tools=None)
    assert not server._agent_verifier_reachable(
        read_only=True, allowed_tools=None,
    )
    assert not server._agent_verifier_reachable(
        read_only=False, allowed_tools=frozenset({"web_search"}),
    )
    assert server._agent_verifier_reachable(
        read_only=False, allowed_tools=frozenset({"web_search", "test_run"}),
    )


def test_an_unreadable_record_demands_verification_rather_than_assuming(
    monkeypatch,
):
    """Below MIN_SAMPLE and below *any* sample both fail closed."""
    def _boom():
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(server, "_open_db", _boom)
    monkeypatch.setattr(server, "_open_db_readonly", _boom)

    out = _run(monkeypatch, [FINAL])

    assert out.startswith(server._AGENT_UNVERIFIED_PREFIX)


def test_reading_the_standing_never_creates_or_migrates_the_store(
    monkeypatch, tmp_path,
):
    """The last completion path that could block on another process's lock.

    ``finish_final`` is a write lane, so ``/report``'s argument does not carry
    over on its own -- but ``_open_db`` still sets ``busy_timeout=30000`` and
    opens ``init_db`` with ``BEGIN IMMEDIATE``, so asking "what is my standing"
    could wait thirty seconds behind a second Sonder process. Reading a count
    is not a reason to take a write lock.
    """
    missing = tmp_path / "memory.db"
    monkeypatch.setattr(server, "_DB_PATH", str(missing))
    monkeypatch.setattr(
        calibration, "_counts", lambda *a, **k: dict(POOR_RECORD),
    )

    demanded, reason = server._agent_verification_standing()

    assert not missing.exists(), "a standing question must not create a store"
    assert demanded is True
    assert "could not be read" in reason


def test_the_connection_is_closed_even_though_the_loop_continues(monkeypatch):
    conns = _record(monkeypatch, UNMEASURED_RECORD)

    _run(monkeypatch, [FINAL])

    assert conns and all(conn.closed for conn in conns)


# --- the `path` axis: the one this file never varied ------------------------


def test_a_narrowing_path_that_misses_the_change_does_not_cover_it(tmp_path):
    """The axis this file claimed to enforce and never exercised.

    Every coverage test above varies only ``root``. ``_agent_verification_covers``
    keyed on ``root`` alone and justified it in-comment: *"their `path` argument
    narrows which checks run inside it, not what those checks exercise."*

    That justification is refuted by code written in the same lane.
    ``harness_tools`` appends ``path`` straight to the child argv, so ``path``
    decides what the child actually looks at -- measured, it could even point
    outside the root entirely until the confinement added alongside this fix
    (see ``test_harness_root_confinement``). ``path`` is what those checks
    exercise.

    Concretely: the model changes ``payments.py`` at the top of the project,
    then runs the verifier narrowed to ``tests/`` -- a real, passing, in-scope
    verification that examined a different part of the tree. Keyed on ``root``
    that counted as covering the change.
    """
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    changed = project / "payments.py"
    changed.write_text("x = 1\n", encoding="utf-8")
    mutations = [{"path": str(changed)}]

    assert server._agent_verification_covers(
        "test_run", {"root": str(project), "path": "tests"}, mutations,
    ) is False, (
        "a verifier narrowed to tests/ was counted as covering a change to "
        "payments.py, which it never looked at"
    )


def test_a_narrowing_path_that_contains_the_change_still_covers_it(tmp_path):
    """The control. Reading ``path`` must not refuse every real narrowed run.

    This is the half the original comment was right about: ``path`` is empty on
    a default invocation, and a narrowed run that *does* contain the change is
    a genuine verification. Without this test, "return False whenever path is
    set" would satisfy the test above.
    """
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    changed = project / "src" / "payments.py"
    changed.write_text("x = 1\n", encoding="utf-8")
    mutations = [{"path": str(changed)}]

    assert server._agent_verification_covers(
        "test_run", {"root": str(project), "path": "src"}, mutations,
    ) is True
    # And the default invocation -- path empty -- is unchanged.
    assert server._agent_verification_covers(
        "test_run", {"root": str(project), "path": ""}, mutations,
    ) is True
    assert server._agent_verification_covers(
        "test_run", {"root": str(project)}, mutations,
    ) is True


def test_a_narrowing_path_is_read_for_the_no_mutation_case_too(tmp_path):
    """The declared-scope branch must narrow the same way.

    With nothing changed on disk the function compares the run's declared
    project scope against the verifier's scope. If ``path`` narrows that scope
    away from the declared project, the verifier no longer covers the scope the
    run is answerable for.
    """
    project = tmp_path / "proj"
    (project / "sub").mkdir(parents=True)

    assert server._agent_verification_covers(
        "test_run", {"root": str(project), "path": "sub"}, [],
        project_scope=str(project),
    ) is False
    assert server._agent_verification_covers(
        "test_run", {"root": str(project)}, [],
        project_scope=str(project),
    ) is True
