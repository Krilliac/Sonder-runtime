"""Boundary tests for with_schema_coverage in sonder_runtime.domain.schema_policy."""

import server
from sonder_runtime.domain.schema_policy import with_schema_coverage


def test_root_helper_is_identity_preserving_alias():
    assert server._with_schema_coverage is with_schema_coverage


def test_no_gaps_returns_text_unchanged():
    assert with_schema_coverage("clean JSON", []) == "clean JSON"


def test_gaps_appended():
    result = with_schema_coverage("output", [("$.foo", "no type")])
    assert result.startswith("output\n[schema_unverified:")
    assert "$.foo" in result
    assert "no type" in result


def test_empty_gaps_returns_text():
    assert with_schema_coverage("text", ()) == "text"


def test_multiple_gaps():
    gaps = [("$.a", "reason1"), ("$.b", "reason2")]
    result = with_schema_coverage("body", gaps)
    assert "$.a" in result
    assert "$.b" in result
