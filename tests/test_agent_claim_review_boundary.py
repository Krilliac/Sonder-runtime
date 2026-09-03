"""Agent claim review lives in the domain; root names are aliases or delegates."""
import server
from sonder_runtime.domain.agents import claim_review


def _hosted_policy(tool_name, *, unsafe=False):
    if tool_name in {"text_search", "file_read_range", "file_find"}:
        return "ERROR: HOST POLICY: local-only tool"
    return ""


def _open_policy(_tool_name, *, unsafe=False):
    return ""


def test_root_names_are_identity_preserving_aliases():
    assert server._AGENT_NEGATIVE_CLAIM_RE is claim_review.NEGATIVE_CLAIM_RE
    assert server._AGENT_CLAIM_REVIEW_TOOLS is claim_review.CLAIM_REVIEW_TOOLS
    assert server._AGENT_QUOTED_ANCHOR_RE is claim_review.QUOTED_ANCHOR_RE
    assert server._AGENT_HEADING_ANCHOR_RE is claim_review.HEADING_ANCHOR_RE
    assert server._AGENT_TASK_PATH_RE is claim_review.TASK_PATH_RE
    assert server._AGENT_SEARCH_QUERY_RE is claim_review.SEARCH_QUERY_RE
    assert server._agent_task_exact_anchors is claim_review.task_exact_anchors


def test_negative_claims_are_recognized_and_ordinary_negatives_are_not():
    assert claim_review.NEGATIVE_CLAIM_RE.search("The repository does not contain a Makefile")
    assert claim_review.NEGATIVE_CLAIM_RE.search("There are no .cpp files here")
    assert claim_review.NEGATIVE_CLAIM_RE.search("no such file")
    assert not claim_review.NEGATIVE_CLAIM_RE.search("no errors, no changes needed")


def test_exact_anchors_come_from_quotes_and_named_headings():
    task = 'Check whether `render_frame` and "Frame Budget" appear under the Rendering Notes heading; also `render_frame`.'
    assert claim_review.task_exact_anchors(task) == ["render_frame", "Frame Budget", "Rendering Notes"]
    assert claim_review.task_exact_anchors("") == []


def test_review_vocabulary_is_derived_from_the_injected_hosted_policy():
    local = claim_review.claim_review_tools(False, cloud_tool_policy_error=_hosted_policy)
    assert local == claim_review.CLAIM_REVIEW_TOOLS
    hosted = claim_review.claim_review_tools(True, cloud_tool_policy_error=_hosted_policy)
    assert hosted == frozenset({"repository_symbol_index", "project_detect"})
    vocabulary = claim_review.claim_review_vocabulary(True, cloud_tool_policy_error=_hosted_policy)
    assert vocabulary == ("project_detect", "repository_symbol_index")
    assert server._agent_claim_review_vocabulary(False) == tuple(sorted(claim_review.CLAIM_REVIEW_TOOLS))
    assert server._agent_claim_review_vocabulary(True) == ("project_detect", "repository_symbol_index")


def test_exact_negative_action_demands_an_unsearched_anchor_then_stands_down():
    task = "Confirm `render_frame` exists in engine.cpp"
    action = claim_review.exact_negative_action(task, ["directory listing"], cloud_tool_policy_error=_open_policy)
    assert action == {
        "decision": "continue",
        "reason": "the exact task anchor 'render_frame' has not been searched",
        "tool": "text_search",
        "args": {"query": "render_frame", "root": ".", "regex": False, "max_results": 20, "glob": "engine.cpp"},
    }
    searched = ["text search: 'render_frame'\nno matches"]
    assert claim_review.exact_negative_action(task, searched, cloud_tool_policy_error=_open_policy) is None
    failed = ["ERROR: text search: 'render_frame' failed"]
    assert claim_review.exact_negative_action(task, failed, cloud_tool_policy_error=_open_policy) is not None
    assert claim_review.exact_negative_action(task, [], cloud=True, cloud_tool_policy_error=_hosted_policy) is None
    assert claim_review.exact_negative_action("no anchors here", [], cloud_tool_policy_error=_open_policy) is None
    assert server._agent_exact_negative_action(task, [], cloud=True) is None
    assert server._agent_exact_negative_action(task, [])["args"]["query"] == "render_frame"
