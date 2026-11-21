import domain_grounding


def test_evaluate_contains_regex_and_json_field():
    artifact = '{"title":"Sonder","score":7}'
    result = domain_grounding.evaluate(artifact, [
        {"type": "contains", "text": "Sonder"},
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


def test_regex_check_reports_failure_for_non_matching_pattern():
    result = domain_grounding.evaluate("hello world", [
        {"type": "regex", "pattern": r"^world"},
        {"type": "regex", "pattern": r"^hello", "note": "anchored"},
    ])
    assert [r["ok"] for r in result["results"]] == [False, True]


def test_regex_check_invalid_pattern_still_raises():
    import re

    import pytest

    with pytest.raises(re.error):
        domain_grounding.evaluate("x", [{"type": "regex", "pattern": "("}])


def test_regex_check_rejects_overlong_pattern():
    import pytest

    pattern = "a" * (domain_grounding.MAX_REGEX_PATTERN_CHARS + 1)
    with pytest.raises(ValueError, match="exceeds"):
        domain_grounding.evaluate("a", [{"type": "regex", "pattern": pattern}])


def test_catastrophic_regex_is_stopped_at_the_deadline(monkeypatch):
    # ^(a+)+$ backtracks exponentially on a run of 'a' that ends in a
    # non-matching character; 27 characters take seconds in-process.  The
    # check must fail closed once the deadline passes instead of running the
    # pattern to completion.
    import time

    monkeypatch.setattr(domain_grounding, "REGEX_TIMEOUT_SECONDS", 0.5, raising=False)
    started = time.monotonic()
    result = domain_grounding.evaluate("a" * 27 + "!", [
        {"type": "regex", "pattern": r"^(a+)+$"},
    ])
    elapsed = time.monotonic() - started
    assert result["ok"] is False
    assert "timed out" in result["results"][0]["detail"]
    assert elapsed < 5.0
