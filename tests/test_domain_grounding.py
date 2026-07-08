import domain_grounding


def test_evaluate_contains_regex_and_json_field():
    artifact = '{"title":"Trilobite","score":7}'
    result = domain_grounding.evaluate(artifact, [
        {"type": "contains", "text": "Trilobite"},
        {"type": "regex", "pattern": r'"score":\s*7'},
        {"type": "json_field", "path": "score", "equals": 7},
    ])
    assert result["ok"] is True


def test_evaluate_reports_failed_check():
    result = domain_grounding.evaluate("hello", [
        {"type": "not_contains", "text": "hello"},
    ])
    assert result["ok"] is False
    assert "FAIL" in domain_grounding.format_result(result)
