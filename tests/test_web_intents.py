import web_intents


def test_weather_in_my_area_requires_location():
    intent = web_intents.classify("Can you tell me the weather in my area?")

    assert intent == {"kind": "weather", "location": ""}


def test_weather_extracts_city_or_zip_without_time_suffix():
    city = web_intents.classify("What's the weather in Chicago, IL tomorrow?")
    postal = web_intents.classify("Forecast for 60601")

    assert city["location"] == "Chicago, IL"
    assert postal["location"] == "60601"


def test_locationless_weather_question_does_not_extract_scaffolding():
    assert web_intents.classify("whats the weather going to be like today") == {
        "kind": "weather", "location": "",
    }


def test_question_form_can_still_name_an_explicit_location():
    assert web_intents.classify("What's Chicago weather going to be today?") == {
        "kind": "weather", "location": "Chicago",
    }


def test_weather_locations_preserve_straight_and_curly_apostrophes():
    assert web_intents.classify("What's St. John's weather tomorrow?") == {
        "kind": "weather", "location": "St. John's",
    }
    assert web_intents.classify(
        "What\u2019s O\u2019Fallon weather tomorrow?"
    ) == {"kind": "weather", "location": "O\u2019Fallon"}


def test_ambiguous_weather_locations_require_clarification():
    assert web_intents.classify("weather in Chicago or Boston today") == {
        "kind": "weather", "location": "",
    }
    assert web_intents.classify("weather in 60601 or 10001") == {
        "kind": "weather", "location": "",
    }
    assert web_intents.classify("weather in Chicago or 60601") == {
        "kind": "weather", "location": "",
    }


def test_conjunctions_inside_single_place_names_are_preserved():
    assert web_intents.classify("weather in Trinidad and Tobago today") == {
        "kind": "weather", "location": "Trinidad and Tobago",
    }
    assert web_intents.classify("forecast for Brighton and Hove") == {
        "kind": "weather", "location": "Brighton and Hove",
    }


def test_temporal_weather_modifiers_are_not_locations():
    for prompt in (
        "what is the current weather?", "What's today's weather?", "current weather",
    ):
        assert web_intents.classify(prompt) == {"kind": "weather", "location": ""}


def test_meta_and_negated_weather_phrases_do_not_route():
    assert web_intents.classify(
        'Why did the parser extract "whats the" from this weather query?'
    ) is None
    assert web_intents.classify("Do not check the weather in Chicago") is None
    assert web_intents.classify("I said do not check the weather in Chicago") is None
    assert web_intents.classify(
        "Is the string 'weather in Chicago' routed?"
    ) is None
    assert web_intents.classify('"weather in Chicago"') is None
    assert web_intents.classify("He asked 'weather in Chicago'") is None
    assert web_intents.classify(
        "I said don't tell me Chicago weather"
    ) is None


def test_weather_verbs_and_provider_exclusions_keep_live_routing():
    assert web_intents.classify("Please query the weather in Chicago") == {
        "kind": "weather", "location": "Chicago",
    }
    prompt = (
        "Search the web, but do not use weather.com; "
        "get the forecast for Chicago"
    )
    assert web_intents.classify(prompt) == {"kind": "research", "query": prompt}


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
