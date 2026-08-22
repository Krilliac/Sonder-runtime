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


def test_read_only_argument_phrases_reach_the_matching_catalog_tool():
    assert cr.resolve("inspect image ./logo.png") == \
        "/image_inspect ./logo.png"
    assert cr.resolve("preview the data data.csv") == \
        "/data_inspect data.csv"
    assert cr.resolve("list the archive bundle.zip") == \
        "/archive_list bundle.zip"
    assert cr.resolve("check the log logs/app.log") == \
        "/log_inspect logs/app.log"
    assert cr.resolve("show the file digest README.md") == \
        "/file_digest README.md"
    assert cr.resolve("show the directory hash ./src") == \
        "/directory_digest ./src"
    assert cr.resolve("discover tests in ./tests") == \
        "/test_discover ./tests"
    assert cr.resolve("search the web for Sonder Runtime") == \
        "/web_search Sonder Runtime"
    assert cr.resolve("fetch the url https://example.com") == \
        "/web_fetch https://example.com"
    assert cr.resolve("show the policy for file_read") == \
        "/policy_explain file_read"
    assert cr.resolve("show task abc-123") == "/task_show abc-123"
    assert cr.resolve("show task ledger goal-123") == \
        "/task_ledger goal-123"
    assert cr.resolve("show checklist plan-123") == \
        "/checklist_show plan-123"
    assert cr.resolve("show evaluation history") == \
        "/evaluation_history_status"
    assert cr.resolve("verify generated artifact pack report") == \
        "/artifact_verify report"
    assert cr.resolve("check weather for Chicago") == "/weather Chicago"
    assert cr.resolve("show the workspace inventory") == "/inventory"
    assert cr.resolve("list the workspace tree") == "/tree"


def test_argument_expansions_leave_follow_on_work_for_the_agent():
    assert cr.resolve("inspect image logo.png and describe the colors") is None
    assert cr.resolve("search the web for Sonder Runtime and summarize it") is None
    assert cr.resolve("discover tests in ./tests then run them") is None


def test_local_prompt_wrappers_preserve_the_complete_prompt():
    assert cr.resolve("ask several local models: compare parser strategies") == \
        "/ensemble compare parser strategies"
    assert cr.resolve("ask multiple local models to review this design") == \
        "/ensemble review this design"
    assert cr.resolve("offload to a local model: normalize this JSON") == \
        "/offload normalize this JSON"
    assert cr.resolve("offload this local task: draft a test matrix") == \
        "/offload draft a test matrix"
    assert cr.resolve("run a local workbench agent on fix the failing tests") == \
        "/work fix the failing tests"
    assert cr.resolve("work on this task: inspect the failing test") == \
        None


def test_local_prompt_wrappers_require_a_prompt_and_explicit_lane():
    assert cr.resolve("ask several local models") is None
    assert cr.resolve("offload to a local model") is None
    assert cr.resolve("work on this task") is None
    assert cr.resolve("ask several models to compare parser strategies") is None
    assert cr.resolve("verify generated file report.json") is None
    assert cr.resolve("check weather for Chicago and tell me what to wear") is None
