"""feedback — conservative classifier for whether a chat turn is FEEDBACK on the
previous answer (thumbs up/down on what trilobite just said), as opposed to a new
task or question.

Lets the passive-learning loop infer record_outcome() calls from ordinary
follow-up chat ("thanks, that worked" / "no that's wrong") without anyone
having to run /pass or /fail by hand. Mirrors the CONSERVATIVE style of
intents.classify: short-circuit early, never hijack a real request. Stdlib
only.
"""
import re

# Longer messages are new tasks, not feedback on the last turn.
_MAX_WORDS = 8

# Messages that open with a question word or an imperative coding verb are
# almost always a new request, even if a feedback-ish word shows up later
# ("write a function that returns an error message" is not negative feedback).
_GUARD_RE = re.compile(
    r"^(how|what|why|write|make|create|build|add|fix|explain|show|generate|implement)\b"
)

_POSITIVE_PHRASES = [
    "thanks", "thank you", "works", "worked", "that works", "perfect",
    "great", "nice", "awesome", "exactly", "correct", "that did it",
    "good", "that's right", "solved it",
]

_COPIED_PHRASES = ["copied", "copying this", "saved this"]
_USED_PHRASES = ["used it", "using it", "i used it", "applied it"]
_EDITED_PHRASES = ["edited it", "i edited it", "tweaked it", "modified it"]

_NEGATIVE_PHRASES = [
    "no", "nope", "wrong", "that's wrong", "incorrect", "doesn't work",
    "does not work", "didn't work", "still fails", "still broken",
    "still errors", "that's broken", "not right", "that failed",
    "nope still",
]

_POSITIVE_RE = re.compile(
    r"\b(%s)\b" % "|".join(re.escape(p) for p in _POSITIVE_PHRASES)
)
_COPIED_RE = re.compile(
    r"\b(%s)\b" % "|".join(re.escape(p) for p in _COPIED_PHRASES)
)
_USED_RE = re.compile(
    r"\b(%s)\b" % "|".join(re.escape(p) for p in _USED_PHRASES)
)
_EDITED_RE = re.compile(
    r"\b(%s)\b" % "|".join(re.escape(p) for p in _EDITED_PHRASES)
)
_NEGATIVE_RE = re.compile(
    r"\b(%s)\b" % "|".join(re.escape(p) for p in _NEGATIVE_PHRASES)
)


def classify_feedback(text):
    """Return "positive", "negative", or None.

    Conservative: only considers SHORT (<= 8 word) messages, never fires on
    messages shaped like a new question/request, and returns None on any
    ambiguity (both cues present, or neither).
    """
    t = (text or "").strip().lower()
    if not t or len(t.split()) > _MAX_WORDS:
        return None
    if _GUARD_RE.match(t):
        return None

    is_positive = bool(_POSITIVE_RE.search(t))
    is_negative = bool(_NEGATIVE_RE.search(t))

    if is_positive and is_negative:
        return None
    if is_positive:
        return "positive"
    if is_negative:
        return "negative"
    return None


def classify_signal(text):
    """Return a reward signal ("accepted", "rejected", "copied", etc.) or None."""
    t = (text or "").strip().lower()
    if not t or len(t.split()) > _MAX_WORDS or _GUARD_RE.match(t):
        return None
    if _NEGATIVE_RE.search(t):
        return "rejected"
    if _COPIED_RE.search(t):
        return "copied"
    if _USED_RE.search(t):
        return "used"
    if _EDITED_RE.search(t):
        return "edited"
    if _POSITIVE_RE.search(t):
        return "accepted"
    return None
