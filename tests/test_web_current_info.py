"""Implicit-recency current-info routing + fabricated-tool-call guard.

Regression suite for the live-verification failure where 'who won the most
recent super bowl?' got zero tool calls, a stale hallucinated answer, and a
fabricated ```sh web_search ...``` pseudo-command block. Mirrors the
web-refusal suite in test_chat_web_routing.py.
"""
import pytest

import server
import web_intents


def setup_function():
    server.activity_tracker.reset_for_tests()


def _no_model(*args, **kwargs):
    raise AssertionError("plain model generation must not run for this prompt")


# --- classify: implicit-recency event/result questions ------------------------


@pytest.mark.parametrize("prompt", [
    "who won the most recent super bowl?",
    "who won the super bowl?",
    "Who won the World Cup?",
    "winner of the world series this year",
    "who is the reigning F1 championship holder?",
    "who won the latest election?",
])
def test_event_result_questions_classify_as_research(prompt):
    assert web_intents.classify(prompt) == {"kind": "research", "query": prompt}


@pytest.mark.parametrize("prompt", [
    "what is the most recent version of Python?",
    "what's the latest iPhone model?",
    "who is CEO of Twitter as of today?",
])
def test_recency_superlative_questions_classify_as_research(prompt):
    assert web_intents.classify(prompt) == {"kind": "research", "query": prompt}


@pytest.mark.parametrize("prompt", [
    "who won the world cup in 1998?",
    "who won the super bowl in 1995",
    "winner of the 2005 world series?",
])
def test_explicit_past_year_stays_offline(prompt):
    assert web_intents.classify(prompt) is None


def test_current_year_event_question_still_routes():
    import time
    year = time.localtime().tm_year
    prompt = "who won the super bowl in %d?" % year
    assert web_intents.classify(prompt) == {"kind": "research", "query": prompt}


@pytest.mark.parametrize("prompt", [
    "explain how DNS works",
    "write a duration parser in Python",
    "who won you over to static typing?",
])
def test_non_recency_prompts_are_not_hijacked(prompt):
    assert web_intents.classify(prompt) is None


# --- fabricated tool-call detection -------------------------------------------


def test_fabricated_shell_style_web_search_detected():
    reply = (
        "Let me search for that.\n\n```sh\nweb_search \"current news "
        "headline\"\n```\n\nBased on the results, the headline is X."
    )
    assert web_intents.fabricated_tool_call(reply)


def test_fabricated_bare_and_call_style_detected():
    assert web_intents.fabricated_tool_call(
        "```\nweb_search latest super bowl winner\n```"
    )
    assert web_intents.fabricated_tool_call(
        "```python\nweb_fetch('https://example.com/news')\n```"
    )


def test_prose_mention_and_normal_code_are_not_fabricated():
    assert not web_intents.fabricated_tool_call(
        "You can ask me to use the web_search tool."
    )
    assert not web_intents.fabricated_tool_call(
        "```python\nresults = web_search('query')\nprint(results)\n```"
    )
    assert not web_intents.fabricated_tool_call(
        "```python\ndef parse(x):\n    return x.strip()\n```"
    )


# --- post-hoc guard re-dispatch ------------------------------------------------


def test_denial_guard_rewrites_fabricated_tool_call(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(server, "_agent_impl", lambda *a, **k: "LIVE ANSWER")

    replaced = server._web_denial_guard(
        "who won the most recent super bowl?",
        "```sh\nweb_search \"super bowl winner\"\n```\nThe winner was LVII.",
    )

    assert replaced == "LIVE ANSWER"


def test_denial_guard_keeps_fabricated_reply_without_web_intent(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)

    replaced = server._web_denial_guard(
        "write me a CLI wrapper for my search tool",
        "```sh\nweb_search \"example\"\n```",
    )

    assert replaced is None


def test_sonder_impl_routes_implicit_recency_before_model(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(server, "_answer", _no_model)
    calls = []

    def fake_agent(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return "TOOL-BACKED ANSWER"

    monkeypatch.setattr(server, "_agent_impl", fake_agent)

    out = server._sonder_impl(
        "who won the most recent super bowl?", session="none", project="none",
    )

    assert "TOOL-BACKED ANSWER" in out
    assert calls[0][1]["required_tool_names"] == ("web_search", "web_fetch")
    assert "[interaction_id" not in out


def test_sonder_impl_fabricated_reply_is_rewritten_and_discarded(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    # Force the work gate open so the prompt reaches the model path.
    monkeypatch.setattr(server.intents, "classify_work", lambda text: True)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict: ("fake-model", False, True, "sonder"),
    )
    monkeypatch.setattr(
        server, "_answer",
        lambda *a, **k: (
            "```sh\nweb_search \"super bowl winner\"\n```\nIt was LVII.",
            "iid-fab-1",
            None,
        ),
    )
    monkeypatch.setattr(server, "_agent_impl", lambda *a, **k: "LIVE ANSWER")
    discarded = []
    monkeypatch.setattr(
        server, "_discard_interaction", lambda iid: discarded.append(iid),
    )

    out = server._sonder_impl(
        "who won the most recent super bowl?", session="none", project="none",
    )

    assert "LIVE ANSWER" in out
    assert "web_search" not in out
    assert discarded == ["iid-fab-1"]
