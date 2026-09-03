"""Pure claim-review policy for an agent's negative existence claims.

A final answer that denies the existence of a searched-for artifact is only
accepted after the task's exact anchors (quoted literals and named headings)
have been searched. This module owns the negative-claim grammar, the anchor
extraction, the reviewer vocabulary derived from the hosted denial function
and the deterministic exact-search action. The hosted denial function is
injected because it lives with the host-policy refusals. Moved from
``server.py`` in the WP1 Three-Hundred-Fifth Slice with its behaviour
byte-for-byte intact.
"""
from __future__ import annotations

import re


NEGATIVE_CLAIM_RE = re.compile(
    r"\b(?:does not|doesn't|did not|could not|cannot|can't)\s+"
    r"(?:contain|include|find|locate|exist)\b|"
    r"\b(?:not found|no matches?|none found|missing from)\b|"
    # "There are no .cpp files", "contains no source files", "no such file",
    # "found no results" -- existence denials phrased around a SEARCHED-FOR
    # artifact (files/matches/functions/symbols/...), which the plain
    # "no matches"/"does not exist" forms above missed. A workbench agent
    # answering "There are no .cpp files" (while its own directory listing
    # showed 44) sailed past this guard because none of the original phrasings
    # matched. Scoped to concrete search artifacts so ordinary negatives ("no
    # errors", "no changes needed", "no side effects") do NOT trigger a
    # re-verification pass.
    r"\bno\s+(?:such\s+)?(?:[\w.*-]+\s+){0,2}"
    r"(?:files?|matches?|results?|occurrences?|instances?|entries|entry|"
    r"functions?|methods?|classes|class|symbols?|references?|definitions?|"
    r"declarations?|usages?|hits?|records?|rows?|directories|directory|folders?)\b",
    re.IGNORECASE,
)


CLAIM_REVIEW_TOOLS = frozenset({
    "text_search", "file_read_range", "file_find", "repository_symbol_index", "project_detect",
})


QUOTED_ANCHOR_RE = re.compile(
    r"`([^`\r\n]{2,120})`|\"([^\"\r\n]{2,120})\"|\'([^\'\r\n]{2,120})\'"
)


HEADING_ANCHOR_RE = re.compile(
    r"\b(?:its|the|a|an)\s+"
    r"([A-Z][A-Za-z0-9_.:-]*(?:\s+[A-Za-z0-9_.:-]+){0,5})\s+heading\b"
)


TASK_PATH_RE = re.compile(
    r"(?<![\w.-])([A-Za-z0-9_.-]+\.(?:md|txt|py|dart|js|ts|json|yaml|yml|toml|"
    r"cpp|cc|cxx|h|hpp|cs|html|css|svg))(?![\w.-])",
    re.IGNORECASE,
)


SEARCH_QUERY_RE = re.compile(r"text search:\s*'([^'\r\n]+)'", re.IGNORECASE)


def task_exact_anchors(task: str) -> list[str]:
    """Extract explicit literals and named headings worth exact negative search."""
    text = str(task or "")
    anchors = []
    for match in QUOTED_ANCHOR_RE.finditer(text):
        anchor = next((value for value in match.groups() if value), "").strip()
        if anchor and len(anchor.split()) <= 12:
            anchors.append(anchor)
    for match in HEADING_ANCHOR_RE.finditer(text):
        anchor = match.group(1).strip().rstrip(".:")
        if anchor:
            anchors.append(anchor)
    deduped = []
    seen = set()
    for anchor in anchors:
        key = re.sub(r"\s+", " ", anchor).strip().lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(anchor)
    return deduped[:6]


def claim_review_tools(cloud: bool = False, *, cloud_tool_policy_error) -> frozenset:
    """Claim-review tools this run can actually reach, derived from the gate.

    ``CLAIM_REVIEW_TOOLS`` is only the first of two gates a claim-review
    action passes.  On a hosted run ``_cloud_agent_tool_policy_error`` refuses
    every local-only tool one step later, and three of the five claim-review
    tools are local-only -- so a reviewer told to use ``text_search`` /
    ``file_read_range`` / ``file_find`` has, hosted, no working vocabulary at
    all, while ``repository_symbol_index`` and ``project_detect`` sit unnamed.

    Deriving the advertised vocabulary from the denial function rather than
    restating one of its tool sets is what stops the two from drifting again;
    this mirrors ``_agent_tool_help``, which was fixed the same way.

    ``cloud_tool_policy_error(tool_name)`` is the hosted denial function,
    injected because it lives with the stringly host-policy refusals.
    """
    if not cloud:
        return frozenset(CLAIM_REVIEW_TOOLS)
    return frozenset(
        name for name in CLAIM_REVIEW_TOOLS
        if not cloud_tool_policy_error(name)
    )


def claim_review_vocabulary(cloud: bool = False, *, cloud_tool_policy_error) -> tuple:
    """Deterministic ordering for the tool names shown to the reviewer."""
    return tuple(sorted(claim_review_tools(cloud, cloud_tool_policy_error=cloud_tool_policy_error)))


def exact_negative_action(
    task: str, observations, cloud: bool = False, *, cloud_tool_policy_error,
) -> dict | None:
    """Require exact anchor queries before accepting a negative existence claim."""
    # This deterministic action runs before the reviewer model is consulted, so
    # a hardcoded tool name here makes the *host itself* propose a tool the
    # host will then refuse.  ``text_search`` is local-only, so it is dead on
    # every hosted run; fall through to the model reviewer instead of emitting
    # an action that can only produce a policy refusal.
    if "text_search" not in claim_review_tools(cloud, cloud_tool_policy_error=cloud_tool_policy_error):
        return None
    anchors = task_exact_anchors(task)
    if not anchors:
        return None
    exact_queries = set()
    for observation in observations:
        text = str(observation or "")
        if "ERROR:" in text:
            continue
        for match in SEARCH_QUERY_RE.finditer(text):
            exact_queries.add(re.sub(r"\s+", " ", match.group(1)).strip().lower())
    missing = next(
        (
            anchor for anchor in anchors
            if re.sub(r"\s+", " ", anchor).strip().lower() not in exact_queries
        ),
        None,
    )
    if not missing:
        return None
    args = {
        "query": missing,
        "root": ".",
        "regex": False,
        "max_results": 20,
    }
    paths = TASK_PATH_RE.findall(str(task or ""))
    if paths:
        args["glob"] = paths[0]
    return {
        "decision": "continue",
        "reason": "the exact task anchor %r has not been searched" % missing,
        "tool": "text_search",
        "args": args,
    }
