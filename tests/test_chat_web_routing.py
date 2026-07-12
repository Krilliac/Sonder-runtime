import server
import web_intents


def setup_function():
    server.activity_tracker.reset_for_tests()


def _no_model(*args, **kwargs):
    raise AssertionError("plain model generation must not run for this prompt")


def _hint():
    return {
        "success": True,
        "city": "Chicago",
        "region": "Illinois",
        "country": "United States",
        "country_code": "US",
        "latitude": 41.8,
        "longitude": -87.6,
        "timezone": "America/Chicago",
    }


def test_capability_question_reports_enabled_tools_without_model(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)

    output = server.chat_web_response("You have an internet tool, right?")

    assert "web search" in output.lower()
    assert "enabled" in output.lower()


def test_weather_my_area_requires_opt_in_or_manual_place(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)

    output = server.chat_web_response("What's the weather in my area?")

    assert "Allow approximate IP location" in output
    assert "city/state or ZIP" in output


def test_explicit_weather_place_uses_tool_without_location_consent(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    calls = []
    monkeypatch.setattr(
        server, "weather_lookup",
        lambda location: calls.append(location) or "LIVE FORECAST",
    )

    output = server.chat_web_response("Weather in Madison, WI tomorrow")

    assert output == "LIVE FORECAST"
    assert calls == ["Madison, WI"]


def test_enabled_location_supplies_weather_and_discloses_approximation(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    calls = []
    monkeypatch.setattr(
        server, "weather_lookup",
        lambda location: calls.append(location) or "LIVE FORECAST",
    )

    output = server.chat_web_response(
        "What's the weather in my area?",
        location_consent=True,
        location_hint=_hint(),
    )

    assert output.endswith("LIVE FORECAST")
    assert "Approximate location: Chicago, Illinois, United States" in output
    assert "Raw IP: not retained or displayed" in output
    assert calls == ["Chicago, Illinois, United States"]


def test_location_question_needs_consent_then_reports_hint(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)

    denied = server.chat_web_response("Where am I?")
    allowed = server.chat_web_response(
        "Where am I?", location_consent=True, location_hint=_hint(),
    )

    assert "Approximate location is off" in denied
    assert "Approximate location: Chicago" in allowed
    assert "public IP" in allowed


def test_near_me_research_augments_agent_and_requires_web_tool(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    calls = []

    def fake_agent(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return "RESEARCHED"

    monkeypatch.setattr(server, "_agent_impl", fake_agent)

    output = server.chat_web_response(
        "Find coffee near me", location_consent=True, location_hint=_hint(),
    )

    assert output == "RESEARCHED"
    assert "Chicago, Illinois, United States" in calls[0][0]
    assert calls[0][1]["required_tool_names"] == ("web_fetch",)


def test_approximate_location_tool_requires_consent(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    calls = []
    monkeypatch.setattr(
        server.web_tools,
        "approximate_location_lookup",
        lambda: calls.append(True) or _hint(),
    )

    denied = server.approximate_location_lookup(False)
    allowed = server.approximate_location_lookup(True)

    assert denied.startswith("ERROR: explicit location consent")
    assert "Approximate location: Chicago" in allowed
    assert calls == [True]


# --- MCP/REPL surface routing (_sonder_impl) -----------------------------


def test_sonder_impl_routes_weather_without_model(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(server, "_answer", _no_model)
    monkeypatch.setattr(
        server, "weather_lookup",
        lambda location: "LIVE FORECAST for %s" % location,
    )

    out = server._sonder_impl(
        "Weather in Madison, WI tomorrow", session="none", project="none",
    )

    assert "LIVE FORECAST for Madison, WI" in out
    assert "[interaction_id" not in out


def test_sonder_impl_capability_answer_is_not_a_denial(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(server, "_answer", _no_model)

    out = server._sonder_impl(
        "you have a tool to access the internet, right?",
        session="none", project="none",
    )

    assert "enabled" in out.lower()
    assert not web_intents.denies_web_access(out)


def test_sonder_impl_weather_my_area_asks_for_location(monkeypatch):
    monkeypatch.delenv("SONDER_LOCATION_CONSENT", raising=False)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(server, "_answer", _no_model)

    out = server._sonder_impl(
        "can you tell me the weather in my area", session="none", project="none",
    )

    assert "Allow approximate IP location" in out
    assert "city/state or ZIP" in out
    assert not web_intents.denies_web_access(out)


def test_sonder_impl_work_prompts_are_not_hijacked(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict: ("fake-model", False, True, "sonder"),
    )
    monkeypatch.setattr(
        server, "_answer",
        lambda *a, **k: ("def weather_app(): ...", "iid-1", None),
    )

    out = server._sonder_impl(
        "write a weather app in Python", session="none", project="none",
    )

    assert "def weather_app" in out
    assert "[interaction_id: iid-1]" in out


def test_env_location_consent_enables_server_lookup(monkeypatch):
    monkeypatch.setenv("SONDER_LOCATION_CONSENT", "1")
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(
        server.web_tools, "approximate_location_lookup", lambda: _hint(),
    )
    monkeypatch.setattr(server, "_answer", _no_model)
    monkeypatch.setattr(server, "weather_lookup", lambda location: "LIVE FORECAST")

    out = server._sonder_impl(
        "what's the weather in my area?", session="none", project="none",
    )

    assert out.endswith("LIVE FORECAST")
    assert "Approximate location: Chicago" in out


def test_sonder_impl_session_followup_bare_location(monkeypatch):
    monkeypatch.delenv("SONDER_LOCATION_CONSENT", raising=False)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(server, "_answer", _no_model)
    monkeypatch.setattr(
        server, "weather_lookup",
        lambda location: "LIVE FORECAST for %s" % location,
    )
    sid = "web-followup-%s" % server.memory_store.new_id()

    first = server._sonder_impl(
        "what's the weather in my area", session=sid, project="none",
    )
    second = server._sonder_impl("Chicago, IL", session=sid, project="none")

    assert "city/state or ZIP" in first
    assert "LIVE FORECAST for Chicago, IL" in second
    # Routed turns must stay out of the learning loop: no footer on either.
    assert "[interaction_id" not in first
    assert "[interaction_id" not in second


def test_sonder_tool_passes_location_consent(monkeypatch):
    captured = {}

    def fake_impl(prompt, **kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(server, "_sonder_impl", fake_impl)

    server.sonder("hello", location_consent=True)

    assert captured["location_consent"] is True


# --- post-hoc denial guard ---------------------------------------------------


def test_denies_web_access_patterns():
    assert web_intents.denies_web_access(
        "As an AI language model, I don't have access to the internet."
    )
    assert web_intents.denies_web_access("I can't browse the web for you.")
    assert web_intents.denies_web_access(
        "Sorry, I do not have real-time weather data."
    )
    assert not web_intents.denies_web_access("Here's a weather app in Python.")
    assert not web_intents.denies_web_access(
        "Web tools are disabled in the current runtime by SONDER_WEB_TOOLS."
    )


def test_denies_web_access_perform_conduct_do_variants():
    assert web_intents.denies_web_access("I can't perform live web searches.")
    assert web_intents.denies_web_access(
        "I cannot conduct web searches on my own."
    )
    assert web_intents.denies_web_access(
        "Sorry, I can't do a web search for that."
    )
    assert web_intents.denies_web_access("I am unable to run internet queries.")
    assert not web_intents.denies_web_access(
        "Web tools are disabled in the current runtime by SONDER_WEB_TOOLS."
    )
    assert not web_intents.denies_web_access(
        "Here is how web search engines work."
    )


def test_missed_denial_skips_capture_and_footer(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict: ("fake-model", False, True, "sonder"),
    )
    monkeypatch.setattr(
        server, "_answer",
        lambda *a, **k: ("I can't perform live web searches.", "iid-7", None),
    )
    discarded = []
    monkeypatch.setattr(
        server, "_discard_interaction", lambda iid: discarded.append(iid),
    )

    # No positive web intent, so the guard cannot re-dispatch -- but the
    # refusal must still stay out of the learning loop.
    out = server._sonder_impl(
        "explain how DNS works", session="none", project="none",
    )

    assert "can't perform live web searches" in out
    assert "[interaction_id" not in out
    assert discarded == ["iid-7"]


def test_rewritten_denial_discards_captured_refusal(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(server, "_route_chat_web", lambda *a, **k: None)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict: ("fake-model", False, True, "sonder"),
    )
    monkeypatch.setattr(
        server, "_answer",
        lambda *a, **k: ("I don't have access to the internet.", "iid-8", None),
    )
    monkeypatch.setattr(
        server, "weather_lookup", lambda location: "LIVE FORECAST",
    )
    discarded = []
    monkeypatch.setattr(
        server, "_discard_interaction", lambda iid: discarded.append(iid),
    )

    out = server._sonder_impl(
        "check the weather in Madison, WI", session="none", project="none",
    )

    assert "LIVE FORECAST" in out
    assert discarded == ["iid-8"]


# --- intent ordering & work-gate overrides -----------------------------------


def test_current_info_beats_capability_intent():
    prompt = (
        "you have a tool to access the internet, use it to tell me one "
        "current news headline"
    )

    assert web_intents.classify(prompt) == {"kind": "research", "query": prompt}


def test_capability_news_prompt_dispatches_web_research(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    calls = []

    def fake_agent(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return "ONE HEADLINE"

    monkeypatch.setattr(server, "_agent_impl", fake_agent)

    out = server.chat_web_response(
        "you have a tool to access the internet, use it to tell me one "
        "current news headline"
    )

    assert out == "ONE HEADLINE"
    assert calls[0][1]["required_tool_names"] == ("web_fetch",)


def test_explicit_search_detector():
    assert web_intents.explicit_search(
        "search the web for today's top news headline and tell me one"
    )
    assert web_intents.explicit_search("please look it up online")
    assert web_intents.explicit_search("google it for me")
    assert not web_intents.explicit_search("search the repo for TODO markers")
    assert not web_intents.explicit_search("write a weather app in Python")
    assert not web_intents.explicit_search("set up google cloud auth in my repo")


def test_explicit_search_overrides_work_gate(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(server.intents, "classify_work", lambda text: True)
    monkeypatch.setattr(server, "_answer", _no_model)
    monkeypatch.setattr(server, "_agent_impl", lambda *a, **k: "TOP HEADLINE")

    out = server._sonder_impl(
        "search the web for today's top news headline and tell me one",
        session="none", project="none",
    )

    assert "TOP HEADLINE" in out
    assert "[interaction_id" not in out


def test_resolved_weather_thread_news_prompt_is_research():
    history = [
        {"role": "user", "content": "what's the weather in Chicago?"},
        {
            "role": "assistant",
            "content": "Weather for Chicago, Illinois, United States\nNow: Clear",
        },
    ]
    prompt = (
        "you have a tool to access the internet, use it to tell me one "
        "current news headline"
    )

    assert web_intents.classify(prompt, history) == {
        "kind": "research", "query": prompt,
    }


def test_resolved_weather_thread_capability_prompt_stays_capability():
    history = [
        {"role": "user", "content": "what's the weather in Chicago?"},
        {
            "role": "assistant",
            "content": "Weather for Chicago, Illinois, United States\nNow: Clear",
        },
    ]

    # The weather request was fulfilled; a bare capability question must not
    # re-run the Chicago lookup.
    assert web_intents.classify("do you have internet access?", history) == {
        "kind": "capability",
    }


def test_sonder_impl_denial_guard_rewrites_refusal(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    # Force only the pre-model route to miss; the repair path must still honor
    # the real shared work gate.
    monkeypatch.setattr(server, "_route_chat_web", lambda *a, **k: None)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict: ("fake-model", False, True, "sonder"),
    )
    monkeypatch.setattr(
        server, "_answer",
        lambda *a, **k: ("I don't have access to the internet.", "iid-1", None),
    )
    monkeypatch.setattr(
        server, "weather_lookup",
        lambda location: "LIVE FORECAST for %s" % location,
    )

    out = server._sonder_impl(
        "check the weather in Madison, WI", session="none", project="none",
    )

    assert "LIVE FORECAST" in out
    assert "don't have access" not in out
    assert "[interaction_id" not in out


def test_denial_guard_keeps_reply_when_tools_disabled(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)

    replaced = server._web_denial_guard(
        "what's the weather in Madison, WI",
        "I don't have access to the internet.",
    )

    assert replaced is None


def test_denial_guard_requires_positive_web_intent(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)

    replaced = server._web_denial_guard(
        "explain how DNS works",
        "As an AI model, I can't browse the web.",
    )

    assert replaced is None


def test_answer_with_history_denial_guard(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict: ("fake-model", False, True, "sonder"),
    )
    monkeypatch.setattr(
        server, "_answer",
        lambda *a, **k: (
            "As an AI model, I do not have real-time data.", None, None,
        ),
    )
    monkeypatch.setattr(
        server, "weather_lookup",
        lambda location: "LIVE FORECAST for %s" % location,
    )

    out = server._answer_with_history_impl(
        "what's the temperature in Madison, WI right now", [],
    )

    assert "LIVE FORECAST for Madison, WI" in out
    assert "real-time" not in out
