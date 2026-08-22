"""Focused natural-language expansions for catalog commands with aliases."""

import command_router as cr


def test_read_only_catalog_phrases_choose_the_intended_tool():
    assert cr.resolve("show the repo log") == "/repo_log"
    assert cr.resolve("show the task list") == "/task_list"
    assert cr.resolve("list past Sonder sessions") == "/sonder_sessions"
    assert cr.resolve("suggest the best tier") == "/route"
    assert cr.resolve("show context size") == "/contextsize"
    assert cr.resolve("show tool status") == "/toolstatus"


def test_artifact_grounding_preserves_the_guarded_path_argument():
    assert cr.resolve("ground the artifact /tmp/report.json") == \
        "/artifact_ground /tmp/report.json"
    assert cr.resolve("validate artifact ./report.json") == \
        "/artifact_ground ./report.json"


def test_ambiguous_artifact_verification_stays_with_the_agent():
    assert cr.resolve("verify artifact /tmp/report.json") is None
