"""Improvement report rendering lives in the domain; the root name is an alias."""
import server
from sonder_runtime.domain import improvement_report_formatting


def _report(**overrides):
    report = {
        "score": 73,
        "interactions": 120,
        "outcomes": 40,
        "acceptance_percent": 80,
        "reviewed_outcomes": 25,
        "autograded_outcomes": 10,
        "unknown_source_outcomes": 5,
        "learning_health": {
            "outcome_coverage_percent": 33,
            "autograded_positive_percent": 90,
            "positive_percent": 85,
        },
        "lessons": 7,
        "facts": 3,
        "memory_quality": {"duplicates": 1, "vague": 2, "no_embedding": 0},
        "context_status": "healthy",
        "cloud_allowed": True,
        "autopilot": {"available": True, "active": 1, "resumable": 2},
        "mcp_runtime": {"status": "ok", "registered_tools": 208, "refresh_count": 4},
        "issues": [],
    }
    report.update(overrides)
    return report


def test_root_helper_is_an_identity_preserving_alias():
    assert server.format_improvement_report is improvement_report_formatting.format_improvement_report


def test_a_full_report_renders_every_section_in_order():
    text = improvement_report_formatting.format_improvement_report(_report())
    lines = text.splitlines()
    assert lines[0] == "sonder improvement report"
    assert lines[1] == "  readiness score: 73/100"
    assert lines[2] == "  learning: 120 interactions, 40 outcomes, 33% covered"
    assert lines[3] == (
        "    caller-judged: 80% of 25 reviewed | autograded: 90% of 10 | "
        "legacy/unknown provenance: 5 | blended: 85%"
    )
    assert lines[4] == "  memory: 7 lessons, 3 facts, duplicate rows=1, vague=2, missing embeddings=0"
    assert lines[5] == "  context: healthy | hosted/cloud: enabled"
    assert lines[6] == "  autonomy: 1 active | 2 resumable"
    assert lines[7] == "  mcp: ok | 208 tools | 4 atomic refreshes"
    assert lines[8] == "  next improvements:"
    assert len(lines) == 9


def test_unmeasured_acceptance_and_unavailable_autonomy_are_named():
    text = improvement_report_formatting.format_improvement_report(_report(
        acceptance_percent=None, cloud_allowed=False, autopilot={"available": False},
    ))
    assert "    caller-judged: unmeasured | autograded:" in text
    assert "  context: healthy | hosted/cloud: disabled" in text
    assert "  autonomy: unavailable" in text


def test_issues_are_capped_at_eight_with_their_actions():
    issues = [
        {"severity": "warn", "area": "memory", "title": "issue %d" % index, "action": "fix %d" % index}
        for index in range(10)
    ]
    text = improvement_report_formatting.format_improvement_report(_report(issues=issues))
    rendered = [line for line in text.splitlines() if line.startswith("    [")]
    assert rendered == ["    [warn] memory: issue %d" % index for index in range(8)]
    assert "        -> fix 7" in text
    assert "fix 8" not in text


def test_an_empty_report_uses_safe_defaults():
    text = improvement_report_formatting.format_improvement_report({})
    assert "  readiness score: 0/100" in text
    assert "  learning: 0 interactions, 0 outcomes, 0% covered" in text
    assert "    caller-judged: unmeasured | autograded: 0% of 0 | legacy/unknown provenance: 0 | blended: 0%" in text
    assert "  context: unknown | hosted/cloud: disabled" in text
    assert "  autonomy: 0 active | 0 resumable" in text
    assert "  mcp: unknown | 0 tools | 0 atomic refreshes" in text
