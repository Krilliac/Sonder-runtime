"""Conservative preference capture for dynamic personalization.

This is separate from technical lessons. Preferences describe how the user wants
Sonder to behave, speak, or choose defaults. The extractor intentionally
handles clear first-person or imperative preference statements and ignores broad
new tasks.
"""
import re


MAX_PREF_WORDS = 32

_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
_ASSIGN_RE = re.compile(r"\s+")
_KEY_RE = re.compile(r"[^a-z0-9]+")

_PATTERNS = [
    (re.compile(r"\b(?:i|we)\s+prefer(?:\s+that)?\s+(?!not\b)(?P<body>[^.!?\n]+)", re.I), "User prefers %s."),
    (re.compile(r"\b(?:i|we)\s+prefer\s+not\s+to\s+(?P<body>[^.!?\n]+)", re.I), "User does not want Sonder to %s."),
    (re.compile(r"\b(?:i|we)\s+(?:do\s+not|don't)\s+like\s+(?P<body>[^.!?\n]+)", re.I), "User does not like %s."),
    (re.compile(r"\b(?:i|we)\s+like\s+it\s+when\s+(?P<body>[^.!?\n]+)", re.I), "User likes it when %s."),
    (re.compile(r"\bplease\s+always\s+(?P<body>[^.!?\n]+)", re.I), "User wants Sonder to always %s."),
    (re.compile(r"\balways\s+(?P<body>[^.!?\n]+)", re.I), "User wants Sonder to always %s."),
    (re.compile(r"\bfrom\s+now\s+on,?\s+(?P<body>[^.!?\n]+)", re.I), "From now on, %s."),
    (re.compile(r"\b(?:please\s+)?never\s+(?P<body>[^.!?\n]+)", re.I), "User does not want Sonder to %s."),
    (re.compile(r"\bcall\s+me\s+(?P<body>[^.!?\n]+)", re.I), "User wants to be called %s."),
    (re.compile(r"\bmy\s+name\s+is\s+(?P<body>[^.!?\n]+)", re.I), "User's name is %s."),
]

_TASK_GUARD_RE = re.compile(
    r"^(?:(?:please|help\s+me)\s+)?"
    r"(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?:make|create|build|fix|run|compile|generate|write|implement|add|"
    r"remove|delete|review|analyze)\b",
    re.I,
)

_UNSAFE_CONTEXT_RE = re.compile(
    r"(?:^|\s)[A-Za-z]:[\\/]|(?:^|\s)(?:~|\.{1,2})[\\/]|"
    r"(?:^|\s)\\\\[^\\\s]+[\\/]|"
    r"(?:^|\s)/(?:home|Users|etc|var|tmp)/|"
    r"\b(?:canary|marker|secret|password|credential|auth\s+token|"
    r"confidential|private\s+(?:data|detail|constraint|project))\b|"
    r"\b(?:issue|pr|pull\s+request)\s*#?\d+\b|"
    r"\b(?:this|that|current|specific)\s+(?:task|turn|request|answer|audit|"
    r"response|message|conversation|chat|project|repo(?:sitory)?|branch|issue|"
    r"file|session)\b|"
    r"\b(?:for|during|only\s+for)\s+(?:this|the\s+current)\s+"
    r"(?:task|turn|request|answer|audit|project|repo(?:sitory)?|branch|session)\b|"
    r"\b(?:for|in|on)\s+(?:the\s+)?(?:project|repo(?:sitory)?|branch)\s+"
    r"[A-Za-z0-9_.-]+\b|"
    r"\b(?:right\s+now|for\s+now|today|tomorrow|yesterday|"
    r"just\s+this\s+once|one[- ]time|temporar(?:y|ily)|"
    r"this\s+(?:conversation|chat)|until\s+\S+|"
    r"(?:the\s+)?next\s+(?:answer|response|message|turn))\b",
    re.I,
)
_META_QUOTE_RE = re.compile(
    r"\b(?:audit|test|example|parser|extractor|prompt|quoted?|says?|said|"
    r"tell|explain|analyze|review|"
    r"contains?|mentions?)\b[^\n]{0,100}[\"'“”‘’]",
    re.I,
)
_QUOTED_PREFERENCE_RE = re.compile(
    r"(?:[\"“][^\"\n]{0,100}\b(?:i|we)\s+prefer\b[^\"\n]{0,100}[\"”]|"
    r"(?:^|[\s(])'[^'\n]{0,100}\b(?:i|we)\s+prefer\b[^'\n]{0,100}')",
    re.I,
)
_META_REFERENCE_RE = re.compile(
    r"\b(?:phrase|sentence|text|document|example|prompt|input)\b[^\n]{0,100}"
    r"\b(?:i|we)\s+prefer\b|"
    r"\b(?:never|(?:(?:did|do|does)\s+)?not)\s+"
    r"(?:say|said|write|wrote)\b[^\n]{0,100}\b(?:i|we)\s+prefer\b|"
    r"\b(?:he|she|they|you|user|document)\s+"
    r"(?:say|says|said|write|writes|wrote|claim|claims|claimed)\b"
    r"[^\n]{0,100}\b(?:i|we)\s+prefer\b",
    re.I,
)
_INSTRUCTION_OVERRIDE_RE = re.compile(
    r"\b(?:ignore|disregard|override|bypass|forget)\b[^\n]{0,60}"
    r"\b(?:all|any|other|previous|prior|system|developer|safety|security|"
    r"instructions?|rules?|"
    r"policy|policies|guardrails?)\b",
    re.I,
)
_COMMAND_TAIL_RE = re.compile(
    r"(?:[.;]|--)\s*(?:please\s+)?"
    r"(?:ignore|disregard|override|bypass|forget|reveal|expose|leak|send|"
    r"upload|run|execute|delete|read|write|call|use|print|show)\b|"
    r"\b(?:and|then)\s+(?:please\s+)?"
    r"(?:ignore|disregard|override|bypass|forget|reveal|expose|leak|send|"
    r"upload|execute|delete)\b",
    re.I,
)
_PROMPT_CONTROL_RE = re.compile(
    r"<\/?(?:system|developer|assistant)\b|"
    r"\[(?:system|developer|assistant)\]|"
    r"\b(?:system|developer)\s+(?:prompt|message)\b|"
    r"\b(?:jailbreak|prompt\s+injection)\b",
    re.I,
)

_CATEGORY_PATTERNS = (
    ("identity", re.compile(r"\b(?:wants?\s+to\s+be\s+called|name\s+is)\b", re.I)),
    ("shell", re.compile(
        r"\b(?:powershell|pwsh|bash|zsh|cmd(?:\.exe)?|terminal|shell|"
        r"command\s+line|windows\s+commands?|linux\s+commands?)\b", re.I,
    )),
    ("code", re.compile(
        r"(?:\b(?:code|coding|programming|python|javascript|typescript|rust|java|"
        r"cpp|msvc|clang|gcc|cmake|ninja|tests?|pytest|unittest|tabs?|spaces?|"
        r"indentation|type\s+hints?|docstrings?)\b|c\+\+|c#)", re.I,
    )),
    ("ui", re.compile(
        r"\b(?:dark\s+mode|light\s+mode|theme|color\s+scheme|layout|ui|ux)\b",
        re.I,
    )),
    ("workflow", re.compile(
        r"\b(?:ask\s+(?:me\s+)?before|confirm\s+before|approval|commit|push|"
        r"pull\s+request|changelog|documentation|docs|source\s+citations?)\b",
        re.I,
    )),
    ("response_style", re.compile(
        r"\b(?:concise|brief|short|direct|detailed|verbose|bullets?|headings?|"
        r"markdown|emojis?|explain|explanation|tone|formal|casual|"
        r"status\s+updates?|"
        r"mention\s+what\s+changed|show\s+(?:your\s+)?progress)\b", re.I,
    )),
)

_TASK_CATEGORY_PATTERNS = {
    "shell": re.compile(
        r"\b(?:powershell|pwsh|bash|zsh|cmd|terminal|shell|command|script|"
        r"windows|linux|wsl|environment\s+variable)\b", re.I,
    ),
    "code": re.compile(
        r"(?:\b(?:code|implement|build|compile|debug|refactor|function|class|api|"
        r"python|javascript|typescript|rust|java|cpp|msvc|clang|gcc|cmake|ninja|"
        r"test|pytest|repository|repo|module|package)\b|c\+\+|c#)", re.I,
    ),
    "ui": re.compile(
        r"\b(?:ui|ux|interface|page|app|website|theme|color|layout|design)\b",
        re.I,
    ),
    "workflow": re.compile(
        r"\b(?:file|delete|modify|commit|push|pull\s+request|release|deploy|"
        r"source|research|document|docs|workflow|project|repo)\b", re.I,
    ),
}

_TECH_FAMILIES = (
    re.compile(r"(?:\bc\+\+|\b(?:cpp|msvc|clang|gcc|cmake|ninja)\b)", re.I),
    re.compile(r"\bpython|pytest\b", re.I),
    re.compile(r"\b(?:javascript|typescript|node(?:\.js)?)\b", re.I),
    re.compile(r"\brust|cargo\b", re.I),
    re.compile(r"(?:\bc#|\.net\b)", re.I),
)
_SHELL_FAMILIES = (
    re.compile(r"\b(?:powershell|pwsh)\b", re.I),
    re.compile(r"\b(?:bash|zsh|linux|wsl)\b", re.I),
    re.compile(r"\b(?:cmd(?:\.exe)?|windows\s+commands?)\b", re.I),
)
_WORKFLOW_FAMILIES = (
    re.compile(r"\b(?:delete|remove|overwrite|destructive)\b", re.I),
    re.compile(r"\b(?:commit|push|pull\s+request|changelog|release|deploy)\b", re.I),
    re.compile(r"\b(?:source|citation|research)\b", re.I),
    re.compile(r"\b(?:documentation|docs)\b", re.I),
)


def _clean(text):
    if not isinstance(text, str):
        return ""
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


def preference_category(text):
    """Classify a durable behavior/default, or return empty when unsafe."""
    value = _clean(text)
    if (
        not value
        or _UNSAFE_CONTEXT_RE.search(value)
        or _META_QUOTE_RE.search(value)
        or _QUOTED_PREFERENCE_RE.search(value)
        or _INSTRUCTION_OVERRIDE_RE.search(value)
        or _PROMPT_CONTROL_RE.search(value)
        or any(ord(char) < 32 for char in value)
    ):
        return ""
    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.search(value):
            return category
    return ""


def is_stable_preference(text, source_text=None):
    """True only for durable behavior/default text safe for future prompts."""
    source = str(source_text if source_text is not None else text or "")
    if (
        "```" in source
        or _UNSAFE_CONTEXT_RE.search(source)
        or "?" in source
        or _META_REFERENCE_RE.search(source)
        or _QUOTED_PREFERENCE_RE.search(source)
        or _INSTRUCTION_OVERRIDE_RE.search(source)
        or _COMMAND_TAIL_RE.search(source)
        or _PROMPT_CONTROL_RE.search(source)
        or any(ord(char) < 32 and char not in "\t\r\n" for char in source)
    ):
        return False
    if _META_QUOTE_RE.search(source):
        return False
    category = preference_category(text)
    if category == "identity":
        source_match = re.search(
            r"\b(?:call\s+me|my\s+name\s+is|wants?\s+to\s+be\s+called|"
            r"user['’]s\s+name\s+is)\s+(.+?)\s*[!?]*$",
            source,
            re.I,
        )
        match = re.search(
            r"\b(?:called|name\s+is)\s+([^.!?]+)", str(text or ""), re.I
        )
        if not match or not source_match:
            return False
        name = source_match.group(1).strip().rstrip(".")
        words = name.split()
        if (
            not 1 <= len(words) <= 4
            or re.fullmatch(r"[A-Za-zÀ-ɏ'\-’ ]+", name) is None
            or any(word.casefold() in {
                "after", "before", "when", "while", "if", "for", "during",
                "later", "tomorrow", "today",
            } for word in words)
        ):
            return False
    return bool(category)


def preference_applies(text, task):
    """Return whether a safe legacy preference is relevant to this task."""
    if not is_stable_preference(text):
        return False
    category = preference_category(text)
    if not category:
        return False
    if category in {"identity", "response_style"}:
        return True
    task = str(task or "")
    task_pattern = _TASK_CATEGORY_PATTERNS.get(category)
    if task_pattern is None or not task_pattern.search(task):
        return False
    if category == "code":
        families = [family for family in _TECH_FAMILIES if family.search(text)]
        task_families = [family for family in _TECH_FAMILIES if family.search(task)]
        task_shell_families = [
            family for family in _SHELL_FAMILIES if family.search(task)
        ]
        conditional = re.search(r"\b(?:for|when|while|in)\b", text, re.I)
        if families and (
            (task_families and not any(family.search(task) for family in families))
            or (task_shell_families and not task_families)
            or (conditional and not task_families)
        ):
            return False
    if category == "shell":
        families = [family for family in _SHELL_FAMILIES if family.search(text)]
        task_families = [family for family in _SHELL_FAMILIES if family.search(task)]
        task_code_families = [
            family for family in _TECH_FAMILIES if family.search(task)
        ]
        conditional = re.search(r"\b(?:for|when|while|in)\b", text, re.I)
        if families and (
            (task_families and not any(family.search(task) for family in families))
            or (task_code_families and not task_families)
            or (conditional and not task_families)
        ):
            return False
    if category == "workflow":
        families = [family for family in _WORKFLOW_FAMILIES if family.search(text)]
        if families and not any(family.search(task) for family in families):
            return False
    return True


def extract_preferences(text):
    """Return normalized preference strings found in a user turn."""
    if not isinstance(text, str):
        return []
    cleaned = _clean(text)
    if (
        not cleaned
        or _TASK_GUARD_RE.match(cleaned)
        or _UNSAFE_CONTEXT_RE.search(str(text or ""))
        or "?" in text
        or _META_REFERENCE_RE.search(text)
        or _META_QUOTE_RE.search(str(text or ""))
        or _QUOTED_PREFERENCE_RE.search(text)
        or _INSTRUCTION_OVERRIDE_RE.search(text)
        or _COMMAND_TAIL_RE.search(text)
        or _PROMPT_CONTROL_RE.search(text)
    ):
        return []
    found = []
    seen = set()
    for pattern, template in _PATTERNS:
        for match in pattern.finditer(cleaned):
            body = _trim_body(match.group("body"))
            if not body:
                continue
            pref = normalize_preference(template % body)
            if not is_stable_preference(pref, source_text=text):
                continue
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
            "- %s [%s, confidence %.2f, evidence %s, revision %s]"
            % (
                row.get("text", ""),
                status,
                float(row.get("confidence") or 0.0),
                row.get("evidence_count", 0),
                row.get("revision", 1),
            )
        )
    return "\n".join(lines)
