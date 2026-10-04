"""Conservative classification of delegated task intent.

The fleet entry point needs one small decision before it chooses a worker
contract: is the user asking the workers to make a workspace artifact, or to
discuss a problem?  This module deliberately stays lexical and deterministic.
It does not inspect the filesystem, call a model, or import the legacy root
intent router.  Ambiguous language remains advisory so a worker never receives
mutation authority merely because a word such as ``build`` was mentioned.
"""
from __future__ import annotations

import re
from typing import Final, Literal

TaskIntent = Literal["build", "advise"]

_BUILD_WORDS: Final = (
    "build", "create", "implement", "make", "write", "develop", "code",
    "scaffold", "generate", "construct", "produce", "add", "edit", "modify",
    "fix", "repair", "refactor", "automate", "program",
)
_BUILD_RE: Final = re.compile(
    r"\b(?:" + "|".join(map(re.escape, _BUILD_WORDS)) + r")\b", re.IGNORECASE
)
_TARGET_RE: Final = re.compile(
    r"\b(?:a|an|the|this|that|my|our|your|me|it|something|one|some|new)\b"
    r"|\b(?:app|application|game|script|tool|program|site|website|service|"
    r"feature|component|prototype|library|module|utility|workflow|automation|"
    r"file|files|project|integration|patch|fix|implementation|solution|code|"
    r"test|tests|docs|documentation|repository|repo)\b",
    re.IGNORECASE,
)
_ADVICE_RE: Final = re.compile(
    r"\b(?:advise|advice|compare|comparison|critique|design|explain|"
    r"overview|proposal|propose|recommend|recommendation|review|should|"
    r"strategy|suggest|tradeoffs?|what\s+if|why|how\s+(?:do|can|should|would)|"
    r"is\s+there|which|whether)\b",
    re.IGNORECASE,
)
_EXPLICIT_ADVICE_RE: Final = re.compile(
    r"\b(?:just|only)\s+(?:advise|answer|explain|plan|outline|review|discuss)\b"
    r"|\b(?:plan|planning|outline|architecture|design|proposal)\s+only\b"
    r"|\b(?:give|make|write)\s+(?:me\s+)?(?:a\s+)?(?:plan|proposal|outline|"
    r"design|comparison|review|critique)\b"
    r"|\bno\s+(?:tools?|files?|changes?|implementation)\b"
    r"|\b(?:do\s+not|don't|dont|never)\s+(?:build|create|implement|make|write|"
    r"modify|edit|change|touch)\b",
    re.IGNORECASE,
)
_DIRECT_QUESTION_RE: Final = re.compile(
    r"^\s*(?:what|why|when|where|who|which|how|should|would|could|can|is|"
    r"are|do|does|did)\b",
    re.IGNORECASE,
)
_POLITE_BUILD_RE: Final = re.compile(
    r"^\s*(?:please\s+|could\s+you\s+|can\s+you\s+|would\s+you\s+|"
    r"i(?:'d|\s+would)\s+like\s+you\s+to\s+|i\s+want\s+you\s+to\s+)?"
    r"(?:build|create|implement|make|write|develop|code|scaffold|generate|"
    r"construct|produce|add|edit|modify|fix|repair|refactor|automate|program)\b",
    re.IGNORECASE,
)
_CONTENT_TARGETS_RE: Final = re.compile(
    r"\b(?:review|critique|plan|proposal|outline|design|compare|comparison|advice|"
    r"recommendation)\b",
    re.IGNORECASE,
)
_CONTENT_REQUEST_RE: Final = re.compile(
    r"^\s*(?:please\s+|could\s+you\s+|can\s+you\s+|would\s+you\s+)?"
    r"(?:write|make|create|produce|generate|design|give)\s+(?:me\s+)?"
    r"(?:a\s+)?(?:review|critique|plan|proposal|outline|design|comparison|"
    r"recommendation|advice)\b",
    re.IGNORECASE,
)


def _visible_text(task: object) -> str:
    """Return task text with quoted/code examples unable to act as commands."""
    value = re.sub(r"\s+", " ", str(task or "")).strip()
    # A discussed command or pasted snippet is evidence about the question,
    # not authorization to give the worker a writable workspace.
    value = re.sub(r"```.*?```", " ", value, flags=re.DOTALL)
    value = re.sub(r"`[^`]*`", " ", value)
    value = re.sub(r'"(?:\\.|[^"\\])*"', " ", value)
    value = re.sub(r"'(?:\\.|[^'\\])*'", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def classify_task(task: object) -> TaskIntent:
    """Classify a master task as ``build`` or conservatively ``advise``.

    Explicit requests to create or change an artifact win over a polite
    question (``Can you build ...``).  A question about how to build, a review,
    design, comparison, plan-only request, negation, quoted command, or an
    otherwise ambiguous fragment stays ``advise``.
    """
    value = _visible_text(task)
    if not value:
        return "advise"

    if _EXPLICIT_ADVICE_RE.search(value):
        return "advise"

    build = _BUILD_RE.search(value)
    if build is None:
        return "advise"

    # Direct implementation wording is the strongest positive signal.  Keep
    # ``write a review`` and similar content-authoring requests advisory.
    if (
        _POLITE_BUILD_RE.match(value)
        and _TARGET_RE.search(value)
        and not _CONTENT_REQUEST_RE.match(value)
    ):
        return "build"

    if _DIRECT_QUESTION_RE.match(value):
        # Questions about the method or choice of implementation are advice
        # even when they mention a creation verb.
        return "advise"

    explicit_followup = re.search(
        r"\b(?:and|then)\s+(?:build|create|implement|make|write|develop|code|"
        r"scaffold|generate|fix|repair|refactor)\b", value, re.IGNORECASE
    )
    if (_CONTENT_TARGETS_RE.search(value) or _ADVICE_RE.search(value)) and not explicit_followup:
        return "advise"

    # A bare action, worker directive, or noun collision is not enough.  A
    # target or an explicit imperative makes the mutation intent concrete.
    if not _TARGET_RE.search(value):
        return "advise"
    delegated_action = re.match(
        r"^(?:(?:have|ask)\s+(?:the\s+)?(?:fleet|workers?|agents?)\s+(?:to\s+)?"
        r"|fleet(?:\s+workers)?\s*:\s*)"
        r"(?:build|create|implement|make|write|develop|code|scaffold|generate)\b",
        value, re.IGNORECASE,
    )
    return "build" if explicit_followup or delegated_action else "advise"


__all__ = ["TaskIntent", "classify_task"]
