import server


def setup_function():
    server.activity_tracker.reset_for_tests()


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
    assert calls[0][1]["required_tool_names"] == (
        "web_search", "web_fetch",
    )


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
