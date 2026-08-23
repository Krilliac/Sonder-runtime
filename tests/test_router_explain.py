"""command_router.explain: the diagnostic twin of resolve.

Three contracts:

* ``explain`` and ``resolve`` always agree on what a turn resolves to,
  because both run the same pipeline -- the trace is bookkeeping, never a
  second implementation that can drift;
* the ``source`` label names the stage that claimed the turn (tier /
  structured / rule / catalog) or the reason nothing did;
* a catalog refusal carries its evidence: the tied candidates of an
  ambiguous turn, the leftover words of an over-asking turn, and the named
  command a risky or read-verb gate refused.

``explain`` never dispatches anything; it exists so tests, tracing, and a
future "why didn't that run" surface can answer from evidence instead of
re-deriving the resolver's behavior by hand.
"""
import pytest

import command_router as cr


pytestmark = pytest.mark.unit


# A corpus spanning every stage and refusal shape. Kept as one list so the
# agreement property below covers exactly what the labeled tests assert on.
_CORPUS = [
    # resolving: hand-written rules
    "show me your stats",
    "read the file notes.txt",
    "switch to the coder model",
    "cancel all agents",
    # resolving: tier trio
    "which model should handle a lookup table",
    "get a second opinion on the lock ordering",
    # resolving: structured calls
    "use the file_read tool with path=README.md",
    "call the diagnostics command",
    # resolving: generic catalog
    "scan for secrets",
    "delete task abc",
    "what is the task progress",
    # refusing: prose and work
    "how do I cache a parse result",
    "fix the failing API tests",
    "read the file notes.txt and summarize it",
    # refusing: ambiguity and risk gates
    "grep files",
    "show the update status",
    "list the git branches",
    "delete all of the tasks",
    # framing
    "",
    "   ",
    "/stats",
    None,
]


def test_explain_and_resolve_always_agree():
    for turn in _CORPUS:
        report = cr.explain(turn)
        assert report["resolved"] == cr.resolve(turn), turn


def test_report_shape_is_stable():
    for turn in _CORPUS:
        report = cr.explain(turn)
        assert set(report) == {"input", "resolved", "source", "detail"}, turn
        assert isinstance(report["detail"], dict), turn
        assert report["source"] in {
            "empty", "slash", "tier", "structured", "rule", "catalog", "none",
        }, turn


def test_each_stage_reports_its_own_source():
    assert cr.explain("show me your stats")["source"] == "rule"
    assert cr.explain("which model should handle a lookup table")["source"] == "tier"
    assert (
        cr.explain("use the file_read tool with path=README.md")["source"]
        == "structured"
    )
    assert cr.explain("scan for secrets")["source"] == "catalog"
    assert cr.explain("how do I cache a parse result")["source"] == "none"
    assert cr.explain("")["source"] == "empty"
    assert cr.explain("/stats")["source"] == "slash"


def test_a_rule_match_names_the_rule():
    detail = cr.explain("show me your stats")["detail"]
    assert isinstance(detail["index"], int)
    assert "stats" in detail["pattern"]


def test_an_ambiguous_turn_reports_its_tied_candidates():
    report = cr.explain("show the update status")
    assert report["resolved"] is None
    assert report["detail"]["reason"] == "ambiguous"
    assert report["detail"]["candidates"] == ["/status", "/update"]


def test_the_read_verb_gate_reports_the_mutation_it_refused():
    report = cr.explain("list the git branches")
    assert report["resolved"] is None
    assert report["detail"]["reason"] == "read-verb-on-mutation"
    assert report["detail"]["command"] == "/git_branch"


def test_a_loose_risky_match_reports_risky_not_named():
    report = cr.explain("delete all of the tasks")
    assert report["resolved"] is None
    assert report["detail"]["reason"] == "risky-not-named"


def test_leftover_words_are_reported_as_the_over_ask_evidence():
    report = cr.explain("read the file notes.txt and summarize it")
    assert report["resolved"] is None
    assert report["detail"]["reason"] == "unexplained-words"
    assert "summarize" in report["detail"]["leftover"]


def test_a_rule_that_matched_but_declined_is_recorded():
    """The weather rule matches the shape and refuses the follow-on clause.

    resolve() keeps scanning after a declined action; the trace must show the
    decline happened rather than pretending no rule was consulted.
    """
    report = cr.explain("get the weather in Paris and tell me a joke")
    assert report["resolved"] is None
    assert report["detail"].get("declined_rules"), report


def test_prose_reports_the_missing_imperative_opening():
    report = cr.explain("how do I cache a parse result")
    assert report["detail"]["reason"] == "no-imperative-opening"


def test_explain_normalizes_exactly_like_resolve():
    padded = "  show   me   your   stats  "
    assert cr.explain(padded)["input"] == "show me your stats"
    assert cr.explain(padded)["resolved"] == cr.resolve(padded) == "/stats"


# --- the /why console surface ----------------------------------------------


def test_why_is_a_catalogued_safe_native_command():
    """Adding the branch self-registers it; pin the grade it registered at."""
    import command_catalog

    command = command_catalog.by_name("/why")
    assert command is not None
    assert command.native
    assert command.risk == "safe"
    assert command.tool == ""
    assert command.summary


def test_the_console_rendering_shows_the_evidence():
    from sonder_runtime.interfaces.repl import repl as sonder_repl

    ambiguous = sonder_repl._format_route_explanation(
        cr.explain("show the update status")
    )
    assert "ambiguous" in ambiguous
    assert "/status" in ambiguous and "/update" in ambiguous

    resolved = sonder_repl._format_route_explanation(
        cr.explain("show me your stats")
    )
    assert "/stats" in resolved

    leftover = sonder_repl._format_route_explanation(
        cr.explain("read the file notes.txt and summarize it")
    )
    assert "summarize" in leftover
    assert "/file_read" in leftover
