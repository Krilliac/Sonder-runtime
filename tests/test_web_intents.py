import web_intents


def test_weather_in_my_area_requires_location():
    intent = web_intents.classify("Can you tell me the weather in my area?")

    assert intent == {"kind": "weather", "location": ""}


def test_weather_extracts_city_or_zip_without_time_suffix():
    city = web_intents.classify("What's the weather in Chicago, IL tomorrow?")
    postal = web_intents.classify("Forecast for 60601")

    assert city["location"] == "Chicago, IL"
    assert postal["location"] == "60601"


def test_capability_followup_preserves_unresolved_weather_context():
    history = [
        {"role": "user", "content": "weather in my area"},
        {"role": "assistant", "content": "Please provide a city."},
    ]

    intent = web_intents.classify(
        "You have an internet tool; can you call it?", history,
    )

    assert intent == {"kind": "weather", "location": ""}


def test_short_location_reply_after_clarification_continues_weather():
    history = [
        {"role": "user", "content": "weather in my area"},
        {
            "role": "assistant",
            "content": "Send a city/state or ZIP, for example Chicago, IL.",
        },
    ]

    assert web_intents.classify("Springfield, IL", history) == {
        "kind": "weather", "location": "Springfield, IL",
    }


def test_weather_followup_reuses_resolved_location():
    history = [
        {"role": "assistant", "content": "Weather for Chicago, Illinois, United States\nNow: Clear"},
    ]

    assert web_intents.classify("what about tomorrow?", history) == {
        "kind": "weather", "location": "Chicago, Illinois, United States",
    }


def test_explicit_web_and_local_queries_route_conservatively():
    assert web_intents.classify("Search the web for current Python news") == {
        "kind": "research", "query": "Search the web for current Python news",
    }
    assert web_intents.classify("Find good coffee near me") == {
        "kind": "research", "query": "Find good coffee near me",
        "needs_location": True,
    }
    assert web_intents.classify("Where am I?") == {"kind": "location"}
    assert web_intents.classify("Explain a binary search") is None


def test_explicit_web_research_wins_when_subject_mentions_weather():
    prompt = (
        "Search the web for the official Open-Meteo weather API documentation "
        "and report the URL."
    )

    assert web_intents.classify(prompt) == {"kind": "research", "query": prompt}
