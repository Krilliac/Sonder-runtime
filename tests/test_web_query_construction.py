"""News/current-info query construction + web-only research dispatch.

Regression suite for the live-verification failures: 'current news headline'
searched verbatim ranked current.com (a fintech) first, and the research agent
issued a spurious local text_search after web results already answered.
"""
import time

import server
import web_tools


def setup_function():
    server.activity_tracker.reset_for_tests()


def _stamp(now):
    return time.strftime("%B %Y", time.localtime(now))


# --- build_search_query --------------------------------------------------------


def test_news_headline_prompt_becomes_dated_topical_query():
    now = 1767225600  # fixed instant so the month/year anchor is deterministic
    query = web_tools.build_search_query("current news headline", now=now)

    assert "news headline" in query
    assert _stamp(now) in query
    assert "current" not in query.lower().replace(
        _stamp(now).lower(), ""
    )


def test_conversational_filler_is_stripped():
    now = 1767225600
    query = web_tools.build_search_query(
        "you have a tool to access the internet, use it to tell me one "
        "current news headline",
        now=now,
    )

    assert query == "news headline %s" % _stamp(now)


def test_event_question_keeps_topic_words():
    now = 1767225600
    query = web_tools.build_search_query(
        "who won the most recent super bowl?", now=now,
    )

    assert "super bowl" in query
    assert _stamp(now) in query


def test_non_recency_queries_are_returned_verbatim():
    assert web_tools.build_search_query("Find coffee near me") == (
        "Find coffee near me"
    )
    assert web_tools.build_search_query(
        "python asyncio.TaskGroup cancellation semantics"
    ) == "python asyncio.TaskGroup cancellation semantics"


def test_non_research_intents_are_never_rewritten():
    assert web_tools.build_search_query(
        "current news headline", intent_kind="weather",
    ) == "current news headline"


def test_month_anchor_is_not_duplicated():
    now = 1767225600
    stamp = _stamp(now)
    query = web_tools.build_search_query(
        "latest news about rust %s" % stamp, now=now,
    )

    assert query.lower().count(stamp.lower()) == 1


# --- chat_web_response research dispatch ----------------------------------------


def _dispatch(monkeypatch, prompt):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    calls = []

    def fake_agent(task, **kwargs):
        calls.append((task, kwargs))
        return "RESEARCHED"

    monkeypatch.setattr(server, "_agent_impl", fake_agent)
    out = server.chat_web_response(prompt)
    assert out == "RESEARCHED"
    return calls[0]


def test_research_dispatch_suggests_constructed_query(monkeypatch):
    task, kwargs = _dispatch(monkeypatch, "current news headline")

    assert "current news headline" in task  # original question kept as task
    assert "Suggested web_search query" in task
    assert "news headline" in task
    assert kwargs["required_tool_names"] == ("web_search", "web_fetch")


def test_research_dispatch_is_web_tool_only(monkeypatch):
    task, kwargs = _dispatch(monkeypatch, "current news headline")

    assert kwargs["tool_allowlist"] == (
        "web_search", "web_fetch", "weather_lookup",
        "approximate_location_lookup",
    )
    assert "text_search" not in kwargs["tool_allowlist"]
    assert kwargs["system"] == server._RESEARCH_AGENT_SYSTEM
    assert kwargs["max_steps"] == 4


def test_specific_research_prompt_gets_no_suggestion_line(monkeypatch):
    task, kwargs = _dispatch(
        monkeypatch, "search the web for python asyncio.TaskGroup docs",
    )

    assert "Suggested web_search query" not in task
    assert kwargs["tool_allowlist"] is not None


def test_agent_impl_accepts_research_system_override(monkeypatch):
    """The system override must reach _build_system instead of the workspace
    agent prompt that invites text_search on pure web questions."""
    captured = {}

    def fake_build_system(text, trace, persona):
        captured["system"] = text
        return text

    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_build_system", fake_build_system)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict: ("fake-model", False, True, "code"),
    )

    def fake_generate(*a, **k):
        raise RuntimeError("stop before any model call")

    monkeypatch.setattr(server, "_make_generate", fake_generate)

    try:
        server._agent_impl("q", system=server._RESEARCH_AGENT_SYSTEM)
    except RuntimeError:
        pass

    assert captured["system"] == server._RESEARCH_AGENT_SYSTEM
    assert "workspace_inventory" not in captured["system"]
