"""Natural phrasing reaches Sonder's read-only capability/self-report tools.

The slice under test is deliberately narrow: "what am I talking to, and how is
it doing" questions resolve to the safe, read-only tools that answer them --
``/tool_manifest``, ``/status``, ``/calibration_status``,
``/learning_health_status``, ``/mcp_runtime_status`` and
``/system_profile_text``.

Three properties are asserted, and the last two matter more than the first:

* the exact phrases resolve (usability),
* nothing they can resolve to is a mutating or otherwise risky command, and a
  turn that carries trailing prose, quoted text or a follow-up action resolves
  to nothing (blast radius),
* a resolved line is still dispatched through the console permission gate, so
  a rule deny refuses it before the handler runs (authorization).
"""
import os
import re

import command_catalog
import command_router as cr
import permission_modes
import server
import sonder_repl


# Every phrase this slice adds, grouped by the command it must resolve to.
# The test that walks this table is also what keeps the doc honest: the same
# phrases appear in docs/NATURAL_LANGUAGE_CAPABILITY_QUERIES.md.
PHRASES = {
    "/tool_manifest": (
        "what tools do you have?",
        "which tools do you have",
        "list your tools",
        "show me your tools",
        "show the tools",
        "list tools",
    ),
    "/status": (
        "what model are you running?",
        "which model are you running",
        "what model is loaded",
        "which models are loaded",
        "what model is running",
        "model status",
    ),
    "/calibration_status": (
        "how reliable are you?",
        "how accurate are you",
        "how well calibrated are you",
        "show your calibration",
        "show me your calibration status",
    ),
    "/learning_health_status": (
        "learning health",
        "show your learning health",
        "show me the learning health status",
        "how is your learning going?",
        "how is your learning doing",
    ),
    "/mcp_runtime_status": (
        "mcp status",
        "show mcp status",
        "show me the mcp runtime status",
    ),
    "/system_profile_text": (
        "show your standing instructions",
        "show me the standing instructions",
        "what are your standing instructions?",
        "show your system profile",
        "what is your system profile",
    ),
}


def test_capability_questions_resolve_to_their_read_only_tool():
    for expected, phrases in PHRASES.items():
        for phrase in phrases:
            assert cr.resolve(phrase) == expected, phrase


def test_every_command_this_slice_can_reach_is_catalogued_safe():
    """A future edit must not point one of these phrases at a mutating tool.

    The phrases read as harmless questions, so the guard is the catalogued
    risk class rather than a reviewer's memory of which tool writes.
    """
    risks = {command.name: command.risk for command in command_catalog.catalog()}
    for name in PHRASES:
        assert risks.get(name) == "safe", name


def test_trailing_prose_quoting_and_follow_up_work_fall_through():
    """A phrase only dispatches when it is the whole turn.

    Retrieved or quoted text must never dispatch, and a request that continues
    past the question is real work whose remainder must not be silently
    discarded.
    """
    for text in (
        "what tools do you have for editing images and can you use one on logo.png",
        "the guide says list your tools but nothing happens",
        'is "list your tools" supposed to work?',
        "how do I list your tools",
        "what model are you running on and can you switch to the coding tier",
        "is your learning health good enough to skip review",
        "show me your calibration data for the last week",
        "what is your system profile made of",
        "explain how mcp status works",
    ):
        assert cr.resolve(text) is None, text


def test_the_mutating_neighbours_are_not_reachable_from_these_phrases():
    """Reading the standing instructions must never become editing them."""
    for text in (
        "update your standing instructions to prefer tabs",
        "set your standing instructions to always use tabs",
        "change the runtime policy to the cloud tier",
    ):
        resolved = cr.resolve(text)
        assert resolved != "/system_profile_text", text
        assert resolved != "/runtime_policy_update", text


def test_existing_phrasings_keep_their_prior_targets():
    """The new rules are additive; the neighbouring surfaces do not move."""
    assert cr.resolve("what tools are installed") == "/env"
    assert cr.resolve("which toolchains are available") == "/env"
    assert cr.resolve("what tools ran") == "/activity"
    assert cr.resolve("show me your stats") == "/stats"
    assert cr.resolve("what can you do") == "/help"


def test_a_resolved_capability_phrase_still_passes_the_permission_gate():
    """Resolution is not authorization.

    The synthesized slash line goes through the same ``permission_modes``
    decision as a typed one, so a ``deny`` refuses it before the tool's
    handler is ever called. The real gate is exercised here (only the policy
    decision is stubbed) so the test would fail if the console stopped
    consulting it.
    """
    line = cr.resolve("list your tools")
    assert line == "/tool_manifest"

    def _never_runs(**_kwargs):
        raise AssertionError("a refused tool was dispatched anyway")

    def _deny(tool_name, **_kwargs):
        return permission_modes.Decision(
            action=permission_modes.DENY, mode=permission_modes.MANUAL,
            risk="safe", reason="denied by rule", tool=tool_name, source="rule",
        )

    original_decide = permission_modes.decide_for_caller
    original_tool = server.tool_manifest
    permission_modes.decide_for_caller = _deny
    server.tool_manifest = _never_runs
    try:
        output = sonder_repl._run_catalogued(line, line)
    finally:
        permission_modes.decide_for_caller = original_decide
        server.tool_manifest = original_tool

    assert "refused" in output
    assert "denied by rule" in output


def _doc_section(title):
    """The lines of one ``## <title>`` section of the capability-query guide."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "docs", "NATURAL_LANGUAGE_CAPABILITY_QUERIES.md",
    )
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    collected, inside = [], False
    for line in lines:
        if line.startswith("## "):
            inside = line[3:].strip().startswith(title)
            continue
        if inside:
            collected.append(line)
    return collected


def test_the_guide_only_promises_phrases_that_actually_resolve():
    """Documented examples are executed, not trusted.

    A doc that lists a phrase the router does not answer is worse than no doc:
    the reader concludes the feature is broken. Parsing the published tables
    keeps the page and the router from drifting apart.
    """
    checked = 0
    for line in _doc_section("Basic"):
        if not line.startswith("|"):
            continue
        columns = line.split("|")
        if len(columns) < 4:
            continue
        phrases = re.findall(r"`([^`]+)`", columns[1])
        target = re.findall(r"`(/[^`]+)`", columns[2])
        if not phrases or not target:
            continue
        for phrase in phrases:
            assert cr.resolve(phrase) == target[0], phrase
            checked += 1
    assert checked >= 15, "the Basic table stopped being parsed"

    for line in _doc_section("Multi-tool"):
        pair = re.match(r"^(\S.*?)\s{2,}->\s*(/\S+)$", line)
        if pair:
            assert cr.resolve(pair.group(1)) == pair.group(2), pair.group(1)
            checked += 1
    assert checked >= 25, "the multi-tool blocks stopped being parsed"


def test_the_guide_only_lists_fall_throughs_that_actually_fall_through():
    checked = 0
    for line in _doc_section("Intermediate"):
        if not line.startswith("|"):
            continue
        columns = line.split("|")
        if len(columns) < 3:
            continue
        phrases = re.findall(r"`([^`]+)`", columns[1])
        if len(phrases) != 1:
            continue
        assert cr.resolve(phrases[0]) is None, phrases[0]
        checked += 1
    assert checked >= 6, "the Intermediate table stopped being parsed"


def test_an_allowed_capability_phrase_reaches_its_handler():
    """The companion to the deny case: nothing is broken when policy allows."""
    line = cr.resolve("mcp status")
    assert line == "/mcp_runtime_status"

    original_tool = server.mcp_runtime_status
    server.mcp_runtime_status = lambda **_kwargs: "mcp runtime converged"
    original_decide = permission_modes.decide_for_caller
    permission_modes.decide_for_caller = lambda *_a, **_k: None
    try:
        output = sonder_repl._run_catalogued(line, line)
    finally:
        permission_modes.decide_for_caller = original_decide
        server.mcp_runtime_status = original_tool

    assert output == "mcp runtime converged"
