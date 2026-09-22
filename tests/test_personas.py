import personas


def test_get_explainer_returns_explainer_prompt():
    assert personas.get("explainer") == personas.PERSONAS["explainer"]


def test_get_unknown_falls_back_to_coder():
    assert personas.get("unknown") == personas.PERSONAS["coder"]


def test_get_none_falls_back_to_coder():
    assert personas.get(None) == personas.PERSONAS["coder"]


def test_get_default_is_coder():
    assert personas.get("") == personas.PERSONAS["coder"]


def test_get_case_and_whitespace_insensitive():
    assert personas.get("  Teacher  ") == personas.PERSONAS["teacher"]


def test_names_includes_all_personas():
    assert personas.names() == [
        "coder", "explainer", "mathematician", "reviewer", "teacher",
    ]


def test_mathematician_keeps_conjecture_separate_from_checked_proof():
    prompt = personas.get("mathematician")
    assert "conjecture" in prompt
    assert "Lean" in prompt
    assert "numerical evidence" in prompt
    assert "constant" in prompt
