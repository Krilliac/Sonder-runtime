"""Conservative routing hints for plain chat turns that need file tools.

This module deliberately does not inspect the filesystem or import the legacy
root ``server`` module.  It only recognizes an explicit local-file question or
an explicit file mutation request; everything else remains ordinary chat.
"""
from __future__ import annotations

import re
from typing import Any


_LOCAL_TARGET = re.compile(
    r"(?:\b(?:workspace|work\s+space|repo(?:sitory)?|codebase|project|"
    r"folder|directory|file|files|scripts?|path|tree|module|package|README|LICENSE|Dockerfile|Makefile)\b|"
    r"(?:[A-Za-z]:[\\/]|[./~][\\/]|\b[\w.-]+\.(?:[A-Za-z]{2,10}|[ch])\b|"
    r"\b(?:[\w.-]+[\\/])+[\w.-]+|\b[a-zA-Z_][\w.-]*_[\w.-]+\b))", re.IGNORECASE,
)

# Explicit mutation verbs.  These are intentionally separate from the broad
# legacy work classifier: a code-writing question such as "how do I write a
# parser?" must not become a filesystem operation.
_MUTATION = re.compile(
    r"(?:create|save|edit|modify|update|write|overwrite|append|rename|move|copy|"
    r"delete|remove|run|execute|deploy|compile|test|fix|patch)\b(?![./\\])",
    re.IGNORECASE,
)
_MUTATION_REQUEST = re.compile(
    r"(?:^(?:/delegate\s+)?(?:(?:please|can\s+you|could\s+you|would\s+you|will\s+you)\s+)*"
    r"|(?:\b(?:and|then|also)\b|[,;])\s+(?:(?:then|also|please)\s+)*)" + _MUTATION.pattern,
    re.IGNORECASE,
)
_MUTATION_TARGET = re.compile(
    r"(?:\b(?:file|files|folder|directory|path|workspace|repo(?:sitory)?|"
    r"codebase|project|script)\b|(?:\b(?:as|to|in|into|named|called)\s+)[`\"']?"
    r"(?:[\w./\\-]+\.(?:[A-Za-z0-9]{1,8})|[A-Za-z]:[\\/][\w./\\-]+|"
    r"[./~][\\/][\w./\\-]+)[`\"']?)",
    re.IGNORECASE,
)
_EXPLICIT_FILE = re.compile(
    r"(?:[A-Za-z]:[\\/]|[./~][\\/]|\b(?:[\w.-]+[\\/])+[\w.-]+|"
    r"\b[\w.-]+\.(?:[A-Za-z]{2,10}|[ch])\b|\b(?:README|LICENSE|Dockerfile|Makefile)\b)",
    re.IGNORECASE,
)
_INSPECTION = re.compile(
    r"\b(?:count|counts|how\s+many|number\s+of|size|sizes|largest|biggest|"
    r"smallest|contents?|what(?:'s|\s+is)\s+in|find|which\s+file|show\s+me|summari[sz]e|"
    r"read|list|inspect|directory\s+tree|file\s+tree|search|look\s+for|"
    r"where\s+is)\b",
    re.IGNORECASE,
)
_LOCAL_FACT_QUESTION = re.compile(
    r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|will\s+you\s+)*"
    r"(?:what(?:'s|\s+is)\s+in|how\s+many|number\s+of|which\s+file|"
    r"what(?:'s|\s+is)?\s+(?:the\s+)?(?:size|largest|biggest|contents?)|"
    r"where\s+is|show\s+me(?!\s+how)|list|find|read|inspect|count|search|look\s+for|summari[sz]e)\b",
    re.IGNORECASE,
)
_QUESTION_OR_EXPLANATION = re.compile(
    r"^(?:how\s+do\s+i|how\s+can\s+i|what\s+is|what\s+are|what\s+does|why\s+|explain\s+|"
    r"teach\s+me|show\s+me\s+how\s+)",
    re.IGNORECASE,
)
_ACTION_PREFIX = re.compile(
    r"^\s*(?:/delegate\s+)?(?:please\s+|can\s+you\s+|could\s+you\s+|"
    r"would\s+you\s+|will\s+you\s+)*(?:create|save|edit|modify|update|"
    r"write|overwrite|append|rename|move|copy|delete|remove|run|execute|"
    r"deploy|compile|test|find|read|show|list|inspect|count)\b",
    re.IGNORECASE,
)
_NO_TOOLS_OR_NEGATION = re.compile(
    r"\b(?:no\s+(?:(?:file|workspace|local)\s+)?tools?|without\s+(?:using\s+)?"
    r"(?:(?:file|workspace|local)\s+)?tools?|do\s+not\s+use\s+"
    r"(?:any\s+)?(?:(?:file|workspace|local)\s+)?tools?|don't\s+use\s+(?:any\s+)?"
    r"(?:(?:file|workspace|local)\s+)?tools?|just\s+answer|"
    r"answer\s+only|never\s+(?:read|write|edit|save|create)|do\s+not\s+"
    r"(?:read|write|edit|save|create)|don't\s+(?:read|write|edit|save|create))\b",
    re.IGNORECASE,
)
_QUOTED = re.compile(r"(?:\"[^\"]*\"|'[^']*'|`[^`]*`)")
_DELEGATE_MUTATION = re.compile(
    r"^\s*/delegate\s+(?:(?:please|can\s+you|could\s+you)\s+)*"
    r"(?:create|save|edit|modify|write|overwrite|append|rename|move|copy)\b",
    re.IGNORECASE,
)
_CREATIVE_TEXT = re.compile(
    r"\b(?:poem|poems|haiku|haikus|limerick|sonnet|song|lyrics|story|stories|"
    r"joke|jokes|riddle|riddles|rap|verse)\b",
    re.IGNORECASE,
)


def _result(mode: str, reason: str, *actions: str) -> dict[str, Any]:
    result: dict[str, Any] = {"mode": mode, "reason": reason, "plan_only": False}
    if actions:
        result["actions"] = list(actions)
    return result


def suppresses_file_tools(text: str) -> bool:
    """Whether a file-related turn explicitly asks to stay out of tools.

    This narrow predicate is intended for the legacy classifier fallback.  It
    does not suppress unrelated work requests, and local-fact questions such
    as ``what is in `sonder_runtime`?`` remain eligible for inspection.
    """
    if not isinstance(text, str):
        return False
    value = text.strip()
    if not value or not _LOCAL_TARGET.search(value):
        return False
    if _NO_TOOLS_OR_NEGATION.search(value):
        return True
    if _LOCAL_FACT_QUESTION.match(value):
        return False
    return bool(_QUESTION_OR_EXPLANATION.match(value))


def classify_file_intent(text: str) -> dict[str, Any] | None:
    """Return a conservative file-tool route for *text*, or ``None``.

    Inspection wins only for read-only requests.  A turn containing both an
    inspection cue and an explicit mutation is sent to the workbench lane so
    it cannot accidentally receive mutation-free tools and then be half done.
    """
    if not isinstance(text, str):
        return None
    value = text.strip()
    if not value or len(value) > 12_000 or _NO_TOOLS_OR_NEGATION.search(value):
        return None

    local_fact = bool(_LOCAL_FACT_QUESTION.match(value) and _LOCAL_TARGET.search(value))

    # Quoted examples and command explanations are conversational material;
    # don't route because a quoted fragment happens to contain "find" or
    # "write".  A quoted filename in an otherwise explicit request is allowed.
    if _QUESTION_OR_EXPLANATION.match(value) and _QUOTED.search(value) and not local_fact:
        return None

    # Local fact questions are eligible even though they begin with "what is"
    # (the generic conceptual guard below must not swallow them).  Backticks
    # and quotes are accepted as literal target delimiters.
    # Conceptual questions about file APIs and code-writing examples belong to
    # chat unless they name an actual local path/file.  This also keeps prose
    # requests such as "write a poem about files" out of the workbench lane.
    has_explicit_file = bool(_EXPLICIT_FILE.search(value))
    if _QUESTION_OR_EXPLANATION.match(value) and not has_explicit_file and not local_fact:
        return None
    if _CREATIVE_TEXT.search(value) and not has_explicit_file:
        return None

    # Quoted examples cannot supply an action cue. Keep their text available
    # below so a quoted filename in a real request still identifies a target.
    mutation = bool(_MUTATION_REQUEST.search(_QUOTED.sub(" ", value)))
    mutation_target = bool(_MUTATION_TARGET.search(value) or _EXPLICIT_FILE.search(value))
    inspection = bool(_INSPECTION.search(value) and _LOCAL_TARGET.search(value))

    # /delegate is a supported natural-language handoff only for an explicit
    # file mutation.  Other slash commands belong to the existing command path.
    if value.startswith("/") and not _DELEGATE_MUTATION.match(value):
        return None
    if _DELEGATE_MUTATION.match(value):
        return _result("workbench", "explicit delegated file mutation", "write")

    # A mutation must be an anchored request, not a historical statement such
    # as "I saved a file yesterday".  Compound "find ... and delete ..."
    # requests are anchored by their first imperative and remain workbench.
    if mutation and mutation_target and (_ACTION_PREFIX.match(value) or local_fact):
        return _result("workbench", "explicit local file mutation", "write")
    if mutation and inspection and (_ACTION_PREFIX.match(value) or local_fact):
        return _result("workbench", "mixed inspection and file mutation", "write")
    if inspection and not mutation and local_fact:
        return _result("inspection", "read-only local workspace inspection", "read")
    return None


__all__ = ["classify_file_intent", "suppresses_file_tools"]
