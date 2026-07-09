"""Conservative preference capture for dynamic personalization.

This is separate from technical lessons. Preferences describe how the user wants
Trilobite to behave, speak, or choose defaults. The extractor intentionally
handles clear first-person or imperative preference statements and ignores broad
new tasks.
"""
import re


MAX_PREF_WORDS = 32

_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
_ASSIGN_RE = re.compile(r"\s+")
_KEY_RE = re.compile(r"[^a-z0-9]+")

_PATTERNS = [
    (re.compile(r"\b(?:i|we)\s+prefer(?:\s+that)?\s+(?P<body>[^.!?\n]+)", re.I), "User prefers %s."),
    (re.compile(r"\b(?:i|we)\s+like\s+it\s+when\s+(?P<body>[^.!?\n]+)", re.I), "User likes it when %s."),
    (re.compile(r"\bplease\s+always\s+(?P<body>[^.!?\n]+)", re.I), "User wants Trilobite to always %s."),
    (re.compile(r"\balways\s+(?P<body>[^.!?\n]+)", re.I), "User wants Trilobite to always %s."),
    (re.compile(r"\bfrom\s+now\s+on,?\s+(?P<body>[^.!?\n]+)", re.I), "From now on, %s."),
    (re.compile(r"\b(?:do\s+not|don't)\s+(?P<body>[^.!?\n]+)", re.I), "User does not want Trilobite to %s."),
    (re.compile(r"\bcall\s+me\s+(?P<body>[^.!?\n]+)", re.I), "User wants to be called %s."),
    (re.compile(r"\bmy\s+name\s+is\s+(?P<body>[^.!?\n]+)", re.I), "User's name is %s."),
    (re.compile(r"\bremember\s+that\s+(?P<body>[^.!?\n]+)", re.I), "Remember that %s."),
]

_TASK_GUARD_RE = re.compile(
    r"^(make|create|build|fix|run|compile|generate|write|implement|add|remove|delete)\b",
    re.I,
)


def _clean(text):
    text = _CODE_FENCE_RE.sub(" ", text or "")
    return _ASSIGN_RE.sub(" ", text).strip()


def _trim_body(body):
    body = _clean(body).strip(" ,;:-")
    words = body.split()
    if not body or len(words) > MAX_PREF_WORDS:
        return ""
    return body


def normalize_preference(text):
    text = _clean(text).strip()
    if not text:
        return ""
    if text[-1:] not in ".!?":
        text += "."
    return text[0].upper() + text[1:]


def preference_key(text):
    base = normalize_preference(text).lower()
    base = _KEY_RE.sub("_", base).strip("_")
    return base[:80] or "preference"


def extract_preferences(text):
    """Return normalized preference strings found in a user turn."""
    cleaned = _clean(text)
    if not cleaned or _TASK_GUARD_RE.match(cleaned):
        return []
    found = []
    seen = set()
    for pattern, template in _PATTERNS:
        for match in pattern.finditer(cleaned):
            body = _trim_body(match.group("body"))
            if not body:
                continue
            pref = normalize_preference(template % body)
            key = preference_key(pref)
            if key not in seen:
                seen.add(key)
                found.append(pref)
    return found


def format_preferences(rows):
    if not rows:
        return "(none)"
    lines = []
    for row in rows:
        status = "on" if int(row.get("enabled", 1)) else "off"
        lines.append(
            "- %s [%s, confidence %.2f, evidence %s]"
            % (
                row.get("text", ""),
                status,
                float(row.get("confidence") or 0.0),
                row.get("evidence_count", 0),
            )
        )
    return "\n".join(lines)
