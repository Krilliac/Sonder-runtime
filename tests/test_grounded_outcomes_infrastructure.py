"""A verifier that could not run measured nothing, and must record nothing.

Task #56, the sibling location of task #12 ("infrastructure failure must not
score as failed"). ``grounded_outcomes.attribute`` took only ``ok``, so a tool
that never started scored identically to one that ran and rejected the work.
Reproduced before the fix::

    go.note_generation("gen-1", "sonder", project="p")
    go.attribute("build_run", ok=False, project="p", record_fn=...)
      -> {"attributed": True, "signal": "failed", "recorded": True}
      -> reward.score("failed") == -1.0

and the entry is *burned*: ``build_run`` joins ``pending.judged``, so the later,
genuine passing ``build_run`` for that same generation comes back
``{"attributed": False, "reason": "no recent generation to judge"}``. One
verifier kind judges a generation once, so an infrastructure blip does not
merely add a wrong row -- it permanently displaces the real verdict.

The evidence exists and was thrown away at the call site. Measured returns from
``harness_tools`` on a project with no build system::

    no build system      {"ok": False, "error": "no recognized build system..."}
    command not found    {"ok": False, "returncode": -1, "stderr": "command not found: ..."}
    unknown framework    {"ok": False, "error": "unknown framework: nosuchfw"}
    real build failure   {"ok": False, "returncode": 1, ...}          <- a verdict

Only the last one is a statement about the work.

Two predicates, one truth
-------------------------
The direct MCP path has the result dict. The agent path does not: ``_agent_
dispatch`` returns rendered text and ``_feed_grounded_outcome`` receives only
that, so a text reader is required for the path that actually runs the agent
and autopilot lanes. It reads exactly the header block ``_format_run_result``
emits and stops at the first ``stdout:``/``stderr:`` line, so a failing test's
own output can never be mistaken for an infrastructure report --
``test_a_failure_whose_output_mentions_errors_is_still_a_verdict`` pins that,
because losing a real negative is worse than keeping a wrong one in a store
already starved of failures. ``test_the_two_predicates_agree`` pins them
against each other so they cannot drift.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grounded_outcomes as go  # noqa: E402
import reward  # noqa: E402
import server  # noqa: E402


@pytest.fixture(autouse=True)
def clean_ledger():
    go.reset()
    yield
    go.reset()


# Measured shapes, taken from live harness_tools returns. Each pair is
# (result dict, is it an infrastructure failure).
HARNESS_RESULTS = [
    ({"ok": False, "error": "no recognized build system found at C:\\p"}, True),
    ({"ok": False, "error": "unknown framework: nosuchfw"}, True),
    ({"ok": False, "error": "unknown linter: nosuch"}, True),
    ({"ok": False, "returncode": -1, "timed_out": True,
      "stdout": "", "stderr": ""}, True),
    ({"ok": False, "returncode": -1, "timed_out": False,
      "stdout": "", "stderr": "command not found: nosuchprog"}, True),
    ({"ok": False, "returncode": 1, "timed_out": False,
      "stdout": "2 failed", "stderr": ""}, False),
    ({"ok": False, "returncode": 2, "timed_out": False,
      "stdout": "SyntaxError", "stderr": ""}, False),
    ({"ok": True, "returncode": 0, "timed_out": False,
      "stdout": "1 passed", "stderr": ""}, False),
]


# --- the dict predicate ----------------------------------------------------

@pytest.mark.parametrize("result,is_infra", HARNESS_RESULTS)
def test_evaluation_infrastructure_error_reads_the_result(result, is_infra):
    detail = go.evaluation_infrastructure_error(result)
    assert bool(detail) is is_infra, result
    if is_infra:
        assert detail.strip(), "an infrastructure refusal must say why"


def test_no_evidence_means_keep_attributing():
    """A verifier that runs no process (an artifact validator) passes none.

    Silence must not be read as "it broke", or the fix would stop every
    unwired verifier from recording anything at all."""
    assert go.evaluation_infrastructure_error(None) == ""
    assert go.evaluation_infrastructure_error("") == ""
    assert go.evaluation_infrastructure_error({}) == ""


# --- the fix, at the ledger ------------------------------------------------

def test_an_infrastructure_failure_records_nothing():
    written = []
    go.note_generation("gen-1", "sonder", project="p")
    report = go.attribute(
        "build_run", ok=False, project="p",
        record_fn=lambda i, s: written.append((i, s)),
        evidence={"ok": False, "error": "no recognized build system found at C:\\p"},
    )
    assert report["attributed"] is False
    assert report["evaluation_infrastructure_error"]
    assert written == []


def test_an_infrastructure_failure_does_not_burn_the_one_shot_entry():
    """The load-bearing half. One verifier kind judges a generation once, so a
    blip that consumed the entry would permanently displace the real verdict."""
    written = []
    go.note_generation("gen-1", "sonder", project="p")
    go.attribute(
        "build_run", ok=False, project="p",
        record_fn=lambda i, s: written.append((i, s)),
        evidence={"ok": False, "returncode": -1, "stderr": "command not found: make"},
    )
    later = go.attribute(
        "build_run", ok=True, project="p",
        record_fn=lambda i, s: written.append((i, s)),
        evidence={"ok": True, "returncode": 0, "stdout": "built"},
    )
    assert later["attributed"] is True
    assert written == [("gen-1", "compiled")]


def test_a_real_failure_is_still_recorded_as_failed():
    """The direction that must NOT change. This store is measurably short of
    negatives; discarding a genuine one is the worse mistake."""
    written = []
    go.note_generation("gen-1", "sonder", project="p")
    report = go.attribute(
        "test_run", ok=False, project="p",
        record_fn=lambda i, s: written.append((i, s)),
        evidence={"ok": False, "returncode": 1, "stdout": "2 failed"},
    )
    assert report["attributed"] is True
    assert written == [("gen-1", "failed")]
    assert reward.score("failed") == -1.0


def test_an_unmeasured_run_is_counted_separately():
    go.note_generation("gen-1", "sonder", project="p")
    go.attribute("build_run", ok=False, project="p",
                 evidence={"ok": False, "error": "no recognized build system"})
    stats = go.stats()
    assert stats["unmeasured"] == 1
    assert stats["attributed"] == 0


def test_evidence_is_optional_and_the_old_behaviour_survives():
    """Verifiers this lane did not wire must keep working exactly as before."""
    written = []
    go.note_generation("gen-1", "sonder", project="p")
    report = go.attribute(
        "artifact_verify", ok=False, project="p",
        record_fn=lambda i, s: written.append((i, s)),
    )
    assert report["attributed"] is True
    assert written == [("gen-1", "rejected")]


# --- the text predicate, for the agent path --------------------------------

@pytest.mark.parametrize("result,is_infra", HARNESS_RESULTS)
def test_the_two_predicates_agree(result, is_infra):
    """Rendered through the real formatter, the text reader must reach the
    same verdict as the dict reader. This is what stops them drifting."""
    rendered = server._format_run_result("build", result)
    assert bool(go.rendered_infrastructure_error(rendered)) is is_infra, rendered


def test_a_failure_whose_output_mentions_errors_is_still_a_verdict():
    """A failing suite prints whatever it likes, including lines that look
    exactly like an infrastructure report. The reader stops at the first
    ``stdout:``/``stderr:`` header for precisely this reason."""
    rendered = server._format_run_result("test run (pytest)", {
        "ok": False, "returncode": 1, "timed_out": False,
        "stdout": "  error: assertion failed\n  timed_out: true\n  returncode: -1",
        "stderr": "E   error: nope",
    })
    assert go.rendered_infrastructure_error(rendered) == ""


def test_a_tool_that_raised_reads_as_infrastructure():
    """``_agent_dispatch`` renders a raised tool as ``ERROR: ...``; the tool
    never produced a verdict about anything."""
    assert go.rendered_infrastructure_error(
        "ERROR: root is outside every authorized root: C:\\x"
    )


@pytest.mark.parametrize("refusal", [
    "ERROR: root is outside every authorized root: C:\\x",
    "ERROR: tool args must be a JSON object",
    "ERROR: read-only agent run has no host-selected project root, so "
    "developer-workflow tool 'secret_scan' has no project to work on. "
    "Pass project=<directory>.",
    "ERROR: HOST POLICY: tool 'x' is refused by the active permission gate.",
    "  ERROR: an indented refusal",
    "ERROR: first line\nstdout: whatever the tool printed",
])
def test_every_dispatcher_refusal_shape_reads_as_infrastructure(refusal):
    """The reader stopped naming the ``ERROR:`` wire marker and reads the
    leading ``error:`` header instead, so pin it against the real refusal
    shapes ``_agent_dispatch`` emits rather than one sample."""
    assert go.rendered_infrastructure_error(refusal) == refusal.strip().splitlines()[0]


@pytest.mark.parametrize("empty_detail", ["ERROR:", "ERROR: ", "  ERROR:  "])
def test_a_refusal_with_no_detail_is_still_infrastructure(empty_detail):
    """``server.isolated_run`` renders ``"ERROR: %s" % exc``, so an exception
    whose ``str()`` is empty produces a refusal with a blank detail. The header
    loop reads the *value* and cannot see it, so deleting the leading read
    rather than replacing it would attribute a verdict the tool never
    produced -- the exact harm this predicate exists to prevent."""
    assert go.rendered_infrastructure_error(empty_detail)


def test_a_verdict_line_is_not_mistaken_for_a_refusal():
    """Only the *leading* line is read as a refusal header. A verifier that
    rendered a real verdict and then mentioned an error must keep its verdict:
    a lost negative is the worse mistake here."""
    assert go.rendered_infrastructure_error(
        "test run (pytest)\n  ok: False\n  returncode: 1"
    ) == ""


def test_unrelated_text_is_not_read_as_infrastructure():
    assert go.rendered_infrastructure_error("") == ""
    assert go.rendered_infrastructure_error("some other tool's output") == ""
    assert go.rendered_infrastructure_error(None) == ""


# --- the formatter must actually carry the reason --------------------------

def test_the_formatter_reports_why_nothing_ran():
    """``_format_run_result`` dropped ``error`` entirely, so a model was told
    ``ok: False`` with no reason and the text reader had nothing to read."""
    rendered = server._format_run_result("build", {
        "ok": False, "error": "no recognized build system found at C:\\p",
    })
    assert "no recognized build system" in rendered


# --- the server wiring -----------------------------------------------------

def test_the_direct_path_passes_evidence(monkeypatch):
    """Verified by executing the real tool, not by reading the call site."""
    seen = {}
    monkeypatch.setattr(go, "attribute", lambda *a, **k: seen.update(k) or {})
    monkeypatch.setattr(go, "VERIFIERS", go.VERIFIERS)
    server._feed_grounded_outcome(
        "build_run", False, "build\n  ok: False",
        {"root": "."}, evidence={"ok": False, "error": "no build system"},
    )
    assert seen.get("evidence") == {"ok": False, "error": "no build system"}


def test_the_agent_path_falls_back_to_the_rendered_text(monkeypatch):
    """The agent and autopilot lanes only ever have text. Without this the
    fix would not reach the path that actually runs."""
    calls = []
    monkeypatch.setattr(go, "attribute",
                        lambda *a, **k: calls.append(k) or {})
    rendered = server._format_run_result("build", {
        "ok": False, "error": "no recognized build system found at C:\\p",
    })
    server._feed_grounded_outcome("build_run", False, rendered, {"root": "."})
    assert calls, "attribute was never reached"
    assert calls[0].get("evidence"), "no evidence derived from the observation"
