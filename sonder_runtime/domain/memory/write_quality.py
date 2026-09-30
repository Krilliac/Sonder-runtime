"""Deterministic write-time quality checks for stored memory text.

Pure classification of one fact or lesson string -- no I/O, no SQLite, no
model call -- so the memory quality report can say how much of the store is
written in a shape that retrieval can use. An entry retrieved into a later
prompt is read without the conversation that wrote it, so the four properties
checked here are the ones that make it usable on its own:

* ``multi_claim``   -- the text is not atomic: it chains several claims, so a
  retrieval that matches one of them drags the others along unasked.
* ``unresolved_reference`` -- the text opens with (or leans on) a pronoun or
  deictic ("this", "it", "the above", "that file") whose antecedent lived in
  the conversation that wrote it and was never stored.
* ``undated_time_sensitive`` -- the text asserts something that expires
  (a version, "currently", "latest") without saying when it was true.
* ``length_out_of_bounds`` -- too short to carry a claim, or too long to be
  one.

Findings are REPORT-ONLY by contract. Nothing in the runtime rewrites or
deletes memory on the strength of these heuristics; they are lexical, so a
flag is a prompt for a human to look, never evidence that the entry is wrong.
"""
from __future__ import annotations

import re

MULTI_CLAIM = "multi_claim"
UNRESOLVED_REFERENCE = "unresolved_reference"
UNDATED_TIME_SENSITIVE = "undated_time_sensitive"
LENGTH_OUT_OF_BOUNDS = "length_out_of_bounds"

#: Stable order used for counters and for every reason list.
CHECKS = (
    MULTI_CLAIM,
    UNRESOLVED_REFERENCE,
    UNDATED_TIME_SENSITIVE,
    LENGTH_OUT_OF_BOUNDS,
)

#: A text with this many claim units (sentences plus clause-level joins) is
#: treated as several claims. Two is allowed on purpose: "Use X. It avoids Y."
#: is one directive plus its reason, the shape a good lesson has.
MAX_CLAIM_UNITS = 2
MIN_WORDS = 3
MAX_CHARS = 280

# Code spans and dotted identifiers are opaque to the sentence splitter:
# "pathlib.Path" or "`a; b`" must not read as two sentences.
_CODE_SPAN = re.compile(r"`[^`]*`")
_SENTENCE_BREAK = re.compile(r"[.!?]+[\"')\]]*\s+(?=[A-Z0-9\"'(\[])")
# Clause-level joins only. A bare " and " between two nouns ("tests and lint")
# is a list, not a second claim, so only the comma/semicolon-marked forms and
# discourse connectives count.
_CLAUSE_JOIN = re.compile(
    r";|,\s*(and|but|so|or|yet)\s|\s(?:but also|and also|as well as|"
    r"additionally|furthermore|moreover|whereas|in addition|plus,)\s",
)
# A serial list's last item ("tests, lint, and docs") is this short; a
# longer segment before ", and" reads as a clause.
_LIST_ITEM_WORDS = 3
_LIST_CLOSERS = frozenset({"and", "or"})
# Abbreviation dots are not sentence ends: "e.g. Ninja" or "Clang vs. MSVC"
# must not split one claim in two. "etc." at a real sentence end then merges
# two sentences -- an undercount, the safe direction for a report-only flag.
_ABBREVIATION = re.compile(
    r"\b(?:e\.g|i\.e|vs|etc|cf|approx|incl|esp|viz|resp)\.", re.IGNORECASE,
)
# Where the backward search for a serial list stops: a semicolon or a
# sentence end.
_LIST_BOUNDARY = re.compile(r";|[.!?]\s")

# A reference at the very start has no antecedent inside the text by
# construction. "This project/repo" is exempt: facts are stored per project,
# so the scope itself resolves it.
_LEADING_REFERENCE = re.compile(
    r"^(?:this|that|these|those|it|its|they|them|their|he|she|his|her|"
    r"here|there|the above|the below|the former|the latter|the same|"
    r"the previous|same|said|such)\b",
)
_SCOPE_RESOLVED = re.compile(
    r"^(?:this|the) (?:project|repo|repository|codebase|workspace|machine|"
    r"user|runtime|store)\b",
)
# "It is safer to ..." / "It's worth ..." -- expletive "it", not a pronoun.
_EXPLETIVE_IT = re.compile(r"^it(?:\s+is|'s)\s+(?:not\s+)?\w+\s+(?:to|that)\b")
# "There is/are ..." is existential, not a place reference.
_EXISTENTIAL_THERE = re.compile(r"^there\s+(?:is|are|was|were|will|should|must)\b")
# Backward references that are unresolved wherever they appear. "that file"
# is deliberately NOT here: mid-sentence "that" is usually a complementizer
# ("Ensure that file paths are absolute"); a leading "That file ..." is still
# caught by _LEADING_REFERENCE.
_BACK_REFERENCE = re.compile(
    r"\b(?:the above|as above|see above|mentioned above|shown above|"
    r"the aforementioned|as mentioned|as discussed|as noted earlier|"
    r"same as before|the previous (?:one|answer|step|message|file|command))\b",
)

_TIME_WORDS = re.compile(
    r"\b(?:currently|current version|right now|at the moment|at present|"
    r"nowadays|these days|as of now|for now|now|latest|newest|most recent|"
    # "not yet" is left out on purpose: it mostly describes program state
    # ("keys not yet present"), not a fact about the world that expires.
    r"recently|today|this (?:week|month|quarter|year)|upcoming|soon|"
    r"no longer)\b",
)
_VERSION = re.compile(
    r"\bv\d+(?:\.\d+)+\b"                       # v1.2, v3.0.1
    r"|\bversion\s+v?\d+"                        # version 5
    r"|[=<>~!]=\s*\d+(?:\.\d+)+"                 # ==1.2, >=2.0
    r"|(?<![\d.])\d+\.\d+\.\d+(?![\d.])",        # 1.2.3, not 10.0.0.1
)
# "Python 3.12", "UE 5.8": a capitalised name followed by a two-part number.
# A number carrying a unit ("Timeout 2.5 seconds", "Wait 1.5 s") is a
# quantity, and a structural or imperative lead word ("Section 3.2",
# "Use 4.0 as ...") names no product, so neither reads as a version.
_NAMED_VERSION = re.compile(
    r"\b([A-Z][\w+#-]*)\s+\d+\.\d+(?![\d.])"
    r"(?!\s*(?i:%|x\b|[kmgt]i?b\b|ms\b|s\b|secs?\b|seconds?\b|mins?\b|"
    r"minutes?\b|h\b|hrs?\b|hours?\b|days?\b|px\b|pt\b|em\b|rem\b))"
)
_NOT_A_PRODUCT = frozenset((
    "use", "set", "wait", "keep", "allow", "try", "pass", "give", "scale",
    "timeout", "section", "chapter", "step", "figure", "fig", "table", "page",
    "rule", "level", "phase", "stage", "item", "part", "appendix", "equation",
    "score", "threshold", "ratio", "weight", "factor", "about", "around",
    # Sentence-initial words that lead a quantity, not a product name.
    "default", "defaults", "return", "returns", "max", "min", "limit",
    "retry", "multiply", "divide", "round", "temperature", "top", "cap",
    "floor", "ceiling", "margin", "budget", "alpha", "decay", "gain",
))
_DATED = re.compile(
    r"\b(?:19|20)\d{2}-\d{2}(?:-\d{2})?\b"      # 2026-08-17, 2026-08
    # "as of" needs a concrete anchor after it: "as of now" is not a date.
    r"|\bas of\s+(?:v?\d|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|q[1-4]\b|(?:version|release|commit)\b)"
    r"|\bsince\s+(?:v?\d|(?:19|20)\d{2})"
    r"|\b(?:in|during|until|before|after)\s+(?:19|20)\d{2}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+"
    r"(?:\d{1,2},?\s+)?(?:19|20)\d{2}\b"
    r"|\bq[1-4]\s+(?:19|20)\d{2}\b"
    r"|\((?:19|20)\d{2}\)",
)


def _strip_code(text: str) -> str:
    return _CODE_SPAN.sub(" CODE ", text)


# The keyword patterns above are written in lower case and compiled WITHOUT
# re.I: every caller matches them against text lowered once, which is ~2-3x
# cheaper than case-insensitive matching and keeps the 10k-row report fast.
# Only _SENTENCE_BREAK and _NAMED_VERSION are case-sensitive on purpose and
# see the original text.


def _clause_joins(lowered_code_free: str) -> int:
    # Joins are visited left to right, so the list boundaries (found once per
    # text) are consumed with a forward-only cursor: the whole count stays
    # linear in the text length. Stored text has no length cap -- MAX_CHARS
    # only flags -- so a per-join rescan of the prefix would be quadratic.
    boundaries = None
    cursor = 0
    start = 0
    joins = 0
    for match in _CLAUSE_JOIN.finditer(lowered_code_free):
        # Only ", and"/", or" can close a serial list; ", but"/", so"/", yet"
        # always join two clauses, however short the segment before them.
        if match.group(1) in _LIST_CLOSERS:
            comma_at = match.start()
            if boundaries is None:
                boundaries = [
                    b.end() for b in _LIST_BOUNDARY.finditer(lowered_code_free)
                ]
            while cursor < len(boundaries) and boundaries[cursor] <= comma_at:
                start = boundaries[cursor]
                cursor += 1
            if _closes_a_list(lowered_code_free, start, comma_at):
                continue
        joins += 1
    return joins


def _closes_a_list(lowered: str, start: int, comma_at: int) -> bool:
    """True when the ", and"/", or" at ``comma_at`` ends a serial list.

    "Run tests, lint, and docs checks" has an Oxford comma, not a second
    clause: an earlier comma in the same sentence and a short item (at most
    ``_LIST_ITEM_WORDS`` words) right before the join. ``start`` is the end of
    the last semicolon or sentence end (".", "!", "?") before the join, so
    "Use X; prefer Y, and run Z" still counts as a join. ``lowered`` has
    abbreviation dots removed, so "e.g." does not end the sentence here either.

    Known undercount: a short introductory clause reads as a list item, so
    "If CI is slow, rerun, and file a bug." scores one join fewer than it
    has. Undercounting is the safe direction for a report-only flag.
    """
    # rfind stops at the nearest comma; every ", and"/", or" join is itself a
    # comma, so successive calls scan disjoint spans (linear overall).
    previous_comma = lowered.rfind(",", start, comma_at)
    if previous_comma < 0:
        return False
    return (
        len(lowered[previous_comma + 1:comma_at].split()) <= _LIST_ITEM_WORDS
    )


def _claim_units(code_free: str, lowered_code_free: str) -> int:
    body = _ABBREVIATION.sub(_drop_final_dot, code_free).strip()
    if not body:
        return 0
    sentences = len([s for s in _SENTENCE_BREAK.split(body) if s.strip()])
    return sentences + _clause_joins(
        _ABBREVIATION.sub(_drop_final_dot, lowered_code_free),
    )


def _drop_final_dot(match: re.Match) -> str:
    return match.group(0)[:-1]


def _unresolved(lowered: str, lowered_code_free: str) -> bool:
    body = lowered.strip().lstrip("-*>#\t ").strip()
    if not body:
        return False
    if _LEADING_REFERENCE.match(body) and not (
        _SCOPE_RESOLVED.match(body)
        or _EXPLETIVE_IT.match(body)
        or _EXISTENTIAL_THERE.match(body)
    ):
        return True
    return bool(_BACK_REFERENCE.search(lowered_code_free))


def _time_sensitive(code_free: str, lowered_code_free: str) -> bool:
    if _TIME_WORDS.search(lowered_code_free) or _VERSION.search(lowered_code_free):
        return True
    return any(
        match.group(1).lower() not in _NOT_A_PRODUCT
        for match in _NAMED_VERSION.finditer(code_free)
    )


def claim_units(text: str) -> int:
    """Sentences plus clause-level joins, code spans ignored."""
    code_free = _strip_code(text or "")
    return _claim_units(code_free, code_free.lower())


def is_multi_claim(text: str) -> bool:
    return claim_units(text) > MAX_CLAIM_UNITS


def has_unresolved_reference(text: str) -> bool:
    text = text or ""
    return _unresolved(text.lower(), _strip_code(text).lower())


def is_time_sensitive(text: str) -> bool:
    code_free = _strip_code(text or "")
    return _time_sensitive(code_free, code_free.lower())


def is_dated(text: str) -> bool:
    return bool(_DATED.search((text or "").lower()))


def is_undated_time_sensitive(text: str) -> bool:
    return is_time_sensitive(text) and not is_dated(text)


def is_length_out_of_bounds(text: str) -> bool:
    body = (text or "").strip()
    return len(body) > MAX_CHARS or len(body.split()) < MIN_WORDS


def classify(text: str) -> list[str]:
    """Reasons ``text`` is badly written for memory, in ``CHECKS`` order.

    Empty text yields no reasons: an empty row is a storage defect the rest of
    the audit owns, not a writing-quality finding. Same verdicts as the four
    public predicates, but code spans are stripped and the text lowered once.
    """
    text = text or ""
    if not text.strip():
        return []
    lowered = text.lower()
    code_free = _strip_code(text)
    lowered_code_free = code_free.lower()
    reasons = []
    if _claim_units(code_free, lowered_code_free) > MAX_CLAIM_UNITS:
        reasons.append(MULTI_CLAIM)
    if _unresolved(lowered, lowered_code_free):
        reasons.append(UNRESOLVED_REFERENCE)
    if _time_sensitive(code_free, lowered_code_free) and not _DATED.search(lowered):
        reasons.append(UNDATED_TIME_SENSITIVE)
    if is_length_out_of_bounds(text):
        reasons.append(LENGTH_OUT_OF_BOUNDS)
    return reasons


def summarize(entries, sample_cap: int = 20) -> dict:
    """Aggregate ``(kind, id, text)`` triples into counters and id samples.

    Samples carry ids and reason names only, never the stored text: facts are
    project-scoped, and this report is read across projects.
    """
    by_check = {name: 0 for name in CHECKS}
    checked = {"fact": 0, "lesson": 0}
    flagged = {"fact": 0, "lesson": 0}
    samples = []
    for kind, entry_id, text in entries:
        checked[kind] = checked.get(kind, 0) + 1
        reasons = classify(text)
        if not reasons:
            continue
        flagged[kind] = flagged.get(kind, 0) + 1
        for reason in reasons:
            by_check[reason] += 1
        if len(samples) < sample_cap:
            samples.append({"kind": kind, "id": entry_id, "reasons": reasons})
    return {
        "checked_facts": checked.get("fact", 0),
        "checked_lessons": checked.get("lesson", 0),
        "flagged_facts": flagged.get("fact", 0),
        "flagged_lessons": flagged.get("lesson", 0),
        "by_check": by_check,
        "samples": samples,
    }
