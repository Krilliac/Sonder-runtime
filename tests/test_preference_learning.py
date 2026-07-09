import preference_learning as P


def test_extract_preferences_from_clear_user_statements():
    prefs = P.extract_preferences(
        "I prefer concise answers. Please always mention what changed."
    )

    assert "User prefers concise answers." in prefs
    assert "User wants Trilobite to always mention what changed." in prefs


def test_extract_preferences_ignores_broad_tasks_and_code():
    assert P.extract_preferences("build a game that says I prefer coins") == []
    assert P.extract_preferences("```python\nprint('I prefer x')\n```") == []


def test_preference_key_is_stable_slug():
    assert P.preference_key("User prefers concise answers.") == "user_prefers_concise_answers"
