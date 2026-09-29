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
    r";|,\s*(?:and|but|so|or|yet)\s|\s(?:but also|and also|as well as|"
    r"additionally|furthermore|moreover|whereas|in addition|plus,)\s",
    re.I,
)

# A reference at the very start has no antecedent inside the text by
# construction. "This project/repo" is exempt: facts are stored per project,
# so the scope itself resolves it.
_LEADING_REFERENCE = re.compile(
    r"^(?:this|that|these|those|it|its|they|them|their|he|she|his|her|"
    r"here|there|the above|the below|the former|the latter|the same|"
    r"the previous|same|said|such)\b",
    re.I,
)
_SCOPE_RESOLVED = re.compile(
    r"^(?:this|the) (?:project|repo|repository|codebase|workspace|machine|"
    r"user|runtime|store)\b",
    re.I,
)
# "It is safer to ..." / "It's worth ..." -- expletive "it", not a pronoun.
_EXPLETIVE_IT = re.compile(r"^it(?:\s+is|'s)\s+(?:not\s+)?\w+\s+(?:to|that)\b", re.I)
# "There is/are ..." is existential, not a place reference.
_EXISTENTIAL_THERE = re.compile(r"^there\s+(?:is|are|was|were|will|should|must)\b", re.I)
# Backward references that are unresolved wherever they appear.
_BACK_REFERENCE = re.compile(
    r"\b(?:the above|as above|see above|mentioned above|shown above|"
    r"the aforementioned|as mentioned|as discussed|as noted earlier|"
    r"same as before|the previous (?:one|answer|step|message|file|command)|"
    r"that (?:file|one|thing|function|error|issue|command))\b",
    re.I,
)

_TIME_WORDS = re.compile(
    r"\b(?:currently|current version|right now|at the moment|at present|"
    r"nowadays|these days|as of now|for now|now|latest|newest|most recent|"
    r"recently|today|this (?:week|month|quarter|year)|upcoming|soon|"
    r"no longer|not yet)\b",
    re.I,
)
_VERSION = re.compile(
    r"\bv\d+(?:\.\d+)+\b"                       # v1.2, v3.0.1
    r"|\bversion\s+v?\d+"                        # version 5
    r"|[=<>~!]=\s*\d+(?:\.\d+)+"                 # ==1.2, >=2.0
    r"|(?<![\d.])\d+\.\d+\.\d+(?![\d.])"         # 1.2.3, not 10.0.0.1
    r"|\b[A-Z][\w+#-]*\s+\d+\.\d+(?![\d.])",     # Python 3.12, UE 5.8
)
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
    re.I,
)


def _strip_code(text: str) -> str:
    return _CODE_SPAN.sub(" CODE ", text)


def claim_units(text: str) -> int:
    """Sentences plus clause-level joins, code spans ignored."""
    body = _strip_code(text or "").strip()
    if not body:
        return 0
    sentences = len([s for s in _SENTENCE_BREAK.split(body) if s.strip()])
    return sentences + len(_CLAUSE_JOIN.findall(body))


def is_multi_claim(text: str) -> bool:
    return claim_units(text) > MAX_CLAIM_UNITS


def has_unresolved_reference(text: str) -> bool:
    body = (text or "").strip().lstrip("-*>#\t ").strip()
    if not body:
        return False
    if _LEADING_REFERENCE.match(body):
        if not (
            _SCOPE_RESOLVED.match(body)
            or _EXPLETIVE_IT.match(body)
            or _EXISTENTIAL_THERE.match(body)
        ):
            return True
    return bool(_BACK_REFERENCE.search(_strip_code(body)))


def is_time_sensitive(text: str) -> bool:
    body = _strip_code(text or "")
    return bool(_TIME_WORDS.search(body) or _VERSION.search(body))


def is_dated(text: str) -> bool:
    return bool(_DATED.search(text or ""))


def is_undated_time_sensitive(text: str) -> bool:
    return is_time_sensitive(text) and not is_dated(text)


def is_length_out_of_bounds(text: str) -> bool:
    body = (text or "").strip()
    return len(body) > MAX_CHARS or len(body.split()) < MIN_WORDS


def classify(text: str) -> list[str]:
    """Reasons ``text`` is badly written for memory, in ``CHECKS`` order.

    Empty text yields no reasons: an empty row is a storage defect the rest of
    the audit owns, not a writing-quality finding.
    """
    if not (text or "").strip():
        return []
    reasons = []
    if is_multi_claim(text):
        reasons.append(MULTI_CLAIM)
    if has_unresolved_reference(text):
        reasons.append(UNRESOLVED_REFERENCE)
    if is_undated_time_sensitive(text):
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
