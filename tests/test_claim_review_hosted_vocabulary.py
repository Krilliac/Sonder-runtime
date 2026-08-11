"""The negative-claim reviewer must not name a vocabulary hosted policy denies.

``_agent_negative_claim_review`` is the mechanism that stops the agent from
returning an unverified negative existence claim ("no such file", "the symbol
does not exist").  Its system prompt and its JSON schema name exactly three
tools -- ``text_search``, ``file_read_range``, ``file_find``.  All three are in
``_CLOUD_AGENT_LOCAL_ONLY_TOOLS``, so on a hosted run every one of them is
refused by ``_cloud_agent_tool_policy_error`` before it reaches dispatch.

That is the admit-then-deny shape applied to a *verification* mechanism: the
surface advertises the tool, the claim-review allowlist admits it, and a second
gate one step later refuses it.  A dispatch-only drift check cannot see it,
because all three tools genuinely dispatch -- just not on this run.

Two independent defects follow, and each is asserted separately below:

1. **Dead vocabulary.**  100 % of the tools the reviewer prompt names are dead
   on a hosted run, while the two claim-review tools that *do* survive hosted
   policy (``repository_symbol_index``, ``project_detect``) are the two the
   prompt never mentions.  The capability exists; the instructions point away
   from it.
2. **A confident verdict the reviewer cannot support.**  When every evidence
   tool it named is refused, nothing records that verification was impossible.
   A reviewer that then returns ``accept`` -- the natural response to two
   rounds of refusals -- lets the bare negative claim through as though it had
   been checked.  A verifier that cannot verify must not return a confident
   verdict.
"""
from __future__ import annotations

import inspect
import re

import pytest

import server


def _hosted_target(*_args, **_kwargs):
    return ("stub-model", True, False, "stub-tier")


def _local_target(*_args, **_kwargs):
    return ("stub-model", False, False, "stub-tier")


def _rendered_reviewer_prompt(monkeypatch, *, cloud):
    """The text the reviewer model is actually shown, system prompt included.

    Deliberately *not* a scan of ``inspect.getsource``: the vocabulary is
    rendered from policy at call time, so the source file no longer contains
    the names, and a source scan would instead match the surrounding prose and
    comments.  What matters is what reaches the model.
    """
    captured = {}

    def fake_build_system(instruction, *args, **kwargs):
        captured["system"] = str(instruction)
        return str(instruction)

    def fake_make_generate(*_args, **_kwargs):
        def generate(prompt, history=None):
            captured["prompt"] = str(prompt)
            return '{"decision":"accept","reason":"done"}'
        return generate

    monkeypatch.setattr(server, "_build_system", fake_build_system)
    monkeypatch.setattr(server, "_make_generate", fake_make_generate)
    server._agent_negative_claim_review(
        # No exact anchor, so the deterministic pre-model action cannot
        # short-circuit and the model reviewer is genuinely reached.
        "look around the workspace",
        "There is no such file anywhere in the tree.",
        ["step 1 tool=file_read reason=look\n(nothing)"],
        "stub-model",
        cloud=cloud,
    )
    assert "system" in captured and "prompt" in captured, (
        "the reviewer model was never reached, so this test measured nothing"
    )
    return captured["system"] + "\n" + captured["prompt"]


def _named_in_reviewer_prompt(monkeypatch, *, cloud):
    text = _rendered_reviewer_prompt(monkeypatch, cloud=cloud)
    return frozenset(
        name for name in server._AGENT_CLAIM_REVIEW_TOOLS
        if re.search(r"\b%s\b" % re.escape(name), text)
    )


# --------------------------------------------------------------------------
# Non-vacuity: these extractors must still see the real thing.
# --------------------------------------------------------------------------

def test_claim_review_extractors_cannot_go_vacuous(monkeypatch):
    assert len(server._AGENT_CLAIM_REVIEW_TOOLS) >= 4
    assert "text_search" in server._AGENT_CLAIM_REVIEW_TOOLS
    # The hosted denial the whole file is about must still be real.
    assert server._cloud_agent_tool_policy_error("text_search")
    assert not server._cloud_agent_tool_policy_error("repository_symbol_index")
    # The prompt reader must see names on both surfaces, or every "names no
    # denied tool" assertion below is a tautology over the empty set.
    local = _named_in_reviewer_prompt(monkeypatch, cloud=False)
    hosted = _named_in_reviewer_prompt(monkeypatch, cloud=True)
    assert len(local) >= 4, local
    assert len(hosted) >= 2, hosted
    assert "text_search" in local
    # And it must see a name planted only in the rendered text.
    text = _rendered_reviewer_prompt(monkeypatch, cloud=True)
    assert "repository_symbol_index" in text


# --------------------------------------------------------------------------
# Defect 1: the reviewer names a vocabulary hosted policy denies.
# --------------------------------------------------------------------------

def test_reviewer_prompt_names_no_tool_hosted_policy_denies(monkeypatch):
    denied = sorted(
        name for name in _named_in_reviewer_prompt(monkeypatch, cloud=True)
        if server._cloud_agent_tool_policy_error(name)
    )
    assert denied == [], (
        "the negative-claim reviewer prompt names %d tool(s) that "
        "_cloud_agent_tool_policy_error refuses on every hosted run, so the "
        "verification mechanism has no working vocabulary there: %s"
        % (len(denied), denied)
    )


def test_reviewer_prompt_names_every_tool_that_survives_hosted_policy(monkeypatch):
    admitted = frozenset(
        name for name in server._AGENT_CLAIM_REVIEW_TOOLS
        if not server._cloud_agent_tool_policy_error(name)
    )
    unnamed = sorted(admitted - _named_in_reviewer_prompt(monkeypatch, cloud=True))
    assert unnamed == [], (
        "these claim-review tools survive hosted policy but the reviewer "
        "prompt never names them, so a hosted reviewer cannot reach the only "
        "verification capability it actually has: %s" % unnamed
    )


def test_local_reviewer_prompt_still_names_its_full_vocabulary(monkeypatch):
    """Narrowing the hosted vocabulary must not narrow the local one."""
    named = _named_in_reviewer_prompt(monkeypatch, cloud=False)
    assert sorted(named) == sorted(server._AGENT_CLAIM_REVIEW_TOOLS)


def test_hosted_claim_review_vocabulary_is_derived_from_policy():
    """The prompt must be rendered from the gate, not restated beside it."""
    hosted = server._agent_claim_review_tools(cloud=True)
    local = server._agent_claim_review_tools(cloud=False)
    assert hosted, "hosted claim review has no admitted evidence tool at all"
    assert hosted < local, "hosted policy is expected to be strictly narrower"
    assert local == frozenset(server._AGENT_CLAIM_REVIEW_TOOLS)
    for name in hosted:
        assert not server._cloud_agent_tool_policy_error(name)


# --------------------------------------------------------------------------
# Defect 2: a verifier that cannot verify must not return a confident verdict.
# --------------------------------------------------------------------------

def _run_agent_with_reviews(monkeypatch, decisions, *, hosted, dispatched):
    monkeypatch.setattr(
        server, "_serve_target", _hosted_target if hosted else _local_target,
    )
    monkeypatch.setattr(
        server,
        "_agent_dispatch_observed",
        lambda tool, *a, **k: (
            dispatched.append(tool) or "### Persistent autopilot"
        ),
    )
    queue = list(decisions)
    monkeypatch.setattr(
        server,
        "_agent_negative_claim_review",
        lambda *a, **k: queue.pop(0) if queue else {"decision": "accept",
                                                   "reason": "exhausted"},
    )
    responses = [
        '{"tool":"file_read","args":{"path":"README.md"}}',
        '{"final":"The Persistent autopilot heading was not found."}',
        '{"final":"The Persistent autopilot heading was not found."}',
        '{"final":"The Persistent autopilot heading was not found."}',
    ]
    monkeypatch.setattr(
        server,
        "_make_generate",
        lambda *a, **k: (lambda prompt, history=None: responses.pop(0)),
    )
    return server._agent_impl(
        "Find the Persistent autopilot heading", max_steps=3,
    )


_ASK_TEXT_SEARCH = {
    "decision": "continue",
    "reason": "the exact anchor was never searched",
    "tool": "text_search",
    "args": {"query": "Persistent autopilot", "root": "."},
}
_ACCEPT = {"decision": "accept", "reason": "no further evidence is obtainable"}


def test_hosted_claim_review_tool_is_refused_before_dispatch(monkeypatch):
    """Baseline reproduction: the named tool never reaches dispatch hosted."""
    hosted_dispatched = []
    _run_agent_with_reviews(
        monkeypatch, [_ASK_TEXT_SEARCH] * 3,
        hosted=True, dispatched=hosted_dispatched,
    )
    assert "text_search" not in hosted_dispatched

    local_dispatched = []
    _run_agent_with_reviews(
        monkeypatch, [_ASK_TEXT_SEARCH] * 3,
        hosted=False, dispatched=local_dispatched,
    )
    assert "text_search" in local_dispatched, (
        "the local run must still verify, or this file is measuring nothing"
    )


def test_accept_after_a_policy_refusal_does_not_return_a_bare_claim(monkeypatch):
    """The reviewer asked for a tool policy refused, then gave up and accepted.

    Nothing was verified.  The run must not hand back the negative claim as
    though it had been checked.
    """
    dispatched = []
    output = _run_agent_with_reviews(
        monkeypatch, [_ASK_TEXT_SEARCH, _ACCEPT],
        hosted=True, dispatched=dispatched,
    )
    assert "text_search" not in dispatched
    assert output.startswith("EVIDENCE_REQUIRED"), (
        "a hosted run whose only claim-review tool was refused by host policy "
        "returned a confident unverified negative claim: %r" % output[:200]
    )


def test_accept_after_successful_verification_is_still_accepted(monkeypatch):
    """The new refusal tracking must not break the case that did verify."""
    dispatched = []
    output = _run_agent_with_reviews(
        monkeypatch,
        [
            {
                "decision": "continue",
                "reason": "check the symbol index",
                "tool": "repository_symbol_index",
                "args": {"root": "."},
            },
            _ACCEPT,
        ],
        hosted=True, dispatched=dispatched,
    )
    assert "repository_symbol_index" in dispatched
    assert not output.startswith("EVIDENCE_REQUIRED")
    assert "not found" in output


def test_local_accept_without_any_refusal_is_unchanged(monkeypatch):
    """No policy refusal happened, so nothing here may change local behaviour."""
    dispatched = []
    output = _run_agent_with_reviews(
        monkeypatch, [_ASK_TEXT_SEARCH, _ACCEPT],
        hosted=False, dispatched=dispatched,
    )
    assert "text_search" in dispatched
    assert not output.startswith("EVIDENCE_REQUIRED")


def test_deterministic_exact_anchor_action_respects_hosted_policy(monkeypatch):
    """``_agent_exact_negative_action`` hardcodes ``text_search``.

    It runs before the reviewer model is ever consulted, so on a hosted run the
    host itself proposes a tool the host will then refuse.
    """
    review = server._agent_negative_claim_review(
        "Inspect README.md and report its Persistent autopilot heading.",
        "The README does not contain a Persistent autopilot heading.",
        [
            "step 1 tool=text_search reason=find\n"
            "text search: 'Persistent autopilot heading' under repo\n(no matches)"
        ],
        "stub-model",
        cloud=True,
    )
    if review["decision"] == "continue" and review["tool"]:
        assert not server._cloud_agent_tool_policy_error(review["tool"]), (
            "the host's own deterministic claim-review action proposes %r, "
            "which hosted policy refuses" % review["tool"]
        )
