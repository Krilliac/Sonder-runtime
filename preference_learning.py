"""Conservative preference capture for dynamic personalization.

This is separate from technical lessons. Preferences describe how the user wants
Sonder to behave, speak, or choose defaults. The extractor intentionally
handles clear first-person or imperative preference statements and ignores broad
new tasks.
"""
import re
import unicodedata


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
_PREFERENCE_CAPTURE_SYNTAX = (
    r"(?:(?:i|we)\s+(?:prefer\b|(?:do\s+not|don't|don’t)\s+like\b|"
    r"like\s+it\s+when\b)|(?:please\s+)?(?:always|never)\b|"
    r"from\s+now\s+on\b|call\s+me\b|my\s+name\s+is\b)"
)
_QUOTED_PREFERENCE_RE = re.compile(
    rf"(?:[\"“][^\"”\n]{{0,300}}{_PREFERENCE_CAPTURE_SYNTAX}"
    rf"[^\"”\n]{{0,300}}[\"”]|(?:^|[\s(])['‘][^'’\n]{{0,300}}"
    rf"{_PREFERENCE_CAPTURE_SYNTAX}[^'’\n]{{0,300}}['’])",
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
    r"policy|policies|guardrails?|safeguards?)\b",
    re.I,
)
_COMMAND_TAIL_RE = re.compile(
    r"(?:[.;:]|--|&|\b(?:and|then)\b)\s*(?:please\s+)?"
    r"(?:(?:also|automatically|directly|immediately|silently)\s+){0,2}"
    r"(?:ignore|disregard|override|bypass|forget|reveal|expose|leak|send|"
    r"upload|run|execute|delete|read|write|call|use|print|show)\b",
    re.I,
)
_PROMPT_CONTROL_RE = re.compile(
    r"<\/?(?:system|developer|assistant)\b|"
    r"\[(?:system|developer|assistant)\]|"
    r"\b(?:system|developer)\s+(?:prompt|message)\b|"
    r"\b(?:jailbreak|prompt\s+injection)\b",
    re.I,
)
_SENSITIVE_DISCLOSURE_RE = re.compile(
    r"\b(?:reveal|expose|leak|disclose|dump|export|send|upload|print|show|read|"
    r"include|attach|transmit|share|quote|enable|report)\b[^\n]{0,120}\b"
    r"(?:environment\s+variables?|env\s+vars?|process\s+environment\s+values?|"
    r"secrets?|passwords?|credentials?|(?:api|access)\s+keys?|session\s+tokens?|"
    r"bearer\s+values?|login\s+details?|connection\s+strings?|"
    r"(?:system|developer)\s+(?:prompts?|messages?|instructions?)|"
    r"hidden\s+(?:data|details?|instructions?)|configuration\s+files?|dotfiles?|"
    r"(?:contents?\s+of\s+)?local\s+files?|(?:diagnostic\s+)?logs?|usage\s+data|"
    r"telemetry(?:\s+identifiers?)?|machine\s+identifiers?|hostnames?|usernames?|"
    r"browser\s+history|clipboard\s+contents?|database\s+rows?|webhooks?|"
    r"command\s+output|private\s+(?:data|details?|information)|"
    r"confidential\s+(?:data|details?|information))\b",
    re.I,
)
_SECURITY_SEPARATOR_RE = re.compile(r"[-_./\\\s]+")
_SENSITIVE_RESOURCE_CANONICAL_RE = re.compile(
    r"\b(?:environment variables?|env vars?|process environment values?|"
    r"secrets?|passwords?|credentials?|(?:api|access) keys?(?: values?)?|"
    r"auth(?:entication)? tokens?|authorization headers?|bearer (?:tokens?|values?)|"
    r"service account tokens?|(?:private|ssh|encryption|wallet) keys?|"
    r"(?:recovery|mfa|multi factor) codes?|cookies?|session (?:ids?|tokens?)|"
    r"seed phrases?|pii|personal (?:data|details?|information)|"
    r"(?:email|ip) addresses?|filesystem paths?|directory listings?|"
    r"process (?:memory|dumps?|lists?)|cloud metadata|"
    r"login details?|connection strings?|"
    r"(?:system|developer) (?:prompts?|messages?|instructions?)|"
    r"hidden (?:data|details?|instructions?)|configuration files?|dotfiles?|"
    r"(?:contents? of )?local files?|local file contents?|"
    r"(?:diagnostic )?logs?|usage data|telemetry(?: identifiers?)?|"
    r"machine identifiers?|hostnames?|usernames?|browser history|"
    r"clipboard contents?|database rows?|webhooks?|command output|"
    r"private (?:data|details?|information)|"
    r"confidential (?:data|details?|information))\b"
)
_BENIGN_RESOURCE_CANONICAL_RE = re.compile(
    r"^(?:(?:i|we) prefer|user prefers) "
    r"(?:powershell|pwsh|bash|zsh|windows|linux) environment variables? "
    r"(?:syntax|notation|naming|names|expansion)\.?$"
)
_SAFE_STYLE_CANONICAL_RE = re.compile(
    r"^(?:user prefers|user likes it when|user does not like|"
    r"user does not want sonder to|user wants sonder to always|from now on) "
    r"(?:(?:(?:concise|brief|short|direct|detailed|verbose|formal|casual|clear) )+"
    r"(?:answers?|responses?|replies|explanations?|reports?|status updates?)|"
    r"step by step (?:answers?|responses?|explanations?)|"
    r"(?:bullet|bulleted) (?:lists?|answers?|responses?)|"
    r"(?:use )?markdown(?: formatting| headings?)?|use emojis?|avoid emojis?|"
    r"mention what changed|show (?:your )?progress)\.?$"
)
_SAFE_SHELL_CANONICAL_RE = re.compile(
    r"^user prefers "
    r"(?:(?:(?:concise|brief|short|direct|detailed|clear) )?"
    r"(?:powershell|pwsh|bash|zsh|cmd|windows|linux) "
    r"(?:commands?|shell|syntax)|"
    r"(?:powershell|pwsh|bash|zsh|windows|linux) environment variables? "
    r"(?:syntax|notation|naming|names|expansion))\.?$"
)
_SAFE_CODE_CANONICAL_RE = re.compile(
    r"^user prefers "
    r"(?:(?:(?:concise|brief|short|direct|detailed|clear) )?"
    r"(?:code|coding|programming)(?: examples?| audits?| style)?|"
    r"(?:(?:concise|brief|short|direct|detailed|clear) )?"
    r"(?:python|javascript|typescript|rust|java|cpp|csharp) "
    r"(?:code|examples?|tests?)|"
    r"(?:python|javascript|typescript|rust|java|cpp|csharp)|"
    r"(?:msvc|clang|gcc) for (?:c|cpp) examples?|"
    r"(?:tabs|spaces) for indentation|type hints|docstrings)\.?$"
)
_SAFE_UI_CANONICAL_RE = re.compile(
    r"^user prefers (?:(?:dark|light) mode|(?:dark|light) theme|"
    r"(?:dark|light) color scheme|compact layout)\.?$"
)
_SAFE_WORKFLOW_CANONICAL_RE = re.compile(
    r"^(?:user prefers|user wants sonder to always|"
    r"user does not want sonder to) "
    r"(?:ask (?:me )?before (?:deleting|removing|overwriting|committing|pushing)|"
    r"confirm before (?:deleting|removing|overwriting|committing|pushing)|"
    r"include source citations|use source citations|"
    r"update (?:documentation|docs|the changelog)|"
    r"mention documentation changes)\.?$"
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
_GENERIC_DURABLE_RE = re.compile(
    r"^(?:user\s+(?:prefers\b|does\s+not\s+(?:want\s+sonder\s+to|like)\b|"
    r"likes\s+it\s+when\b|wants\s+sonder\s+to\s+always\b)|"
    r"from\s+now\s+on\b|(?:i|we)\s+prefer\b)",
    re.I,
)
_SAFE_GENERAL_DEFAULT_RE = re.compile(
    r"^(?:user\s+prefers\s+|user\s+wants\s+sonder\s+to\s+always\s+use\s+|"
    r"from\s+now\s+on,?\s+use\s+)"
    r"(?:(?:metric|imperial|si)\s+units?|iso[ -]?8601\s+dates?|"
    r"(?:12|24)[ -]hour\s+time|(?:utc|local)\s+time(?:\s+zone)?|"
    r"answers?\s+in\s+[a-z][a-z -]{1,24})\.?$",
    re.I,
)

_TOPICAL_DIRECTIVE_RE = re.compile(
    r"^(?:user\s+(?:wants\s+sonder\s+to\s+always|does\s+not\s+want\s+sonder\s+to)\s+|"
    r"from\s+now\s+on,?\s+)"
    r"(?:explain|describe|summarize|analyse|analyze|review|research|discuss|"
    r"teach|solve|calculate|generate|create|build|write|implement|fix)\s+"
    r"(?P<subject>[^.!?]+)",
    re.I,
)
_TOPIC_WORD_RE = re.compile(r"[a-z0-9]+", re.I)
_TOPIC_STOP_WORDS = frozenset({
    "a", "about", "all", "an", "and", "answer", "answers", "as", "at",
    "be", "brief", "briefly", "by", "clear", "clearly", "detailed",
    "direct", "directly", "for", "from", "in", "it", "me", "my", "of",
    "on", "or", "please", "response", "responses", "sonder", "step",
    "steps", "that", "the", "their", "them", "this", "to", "user", "we",
    "what", "when", "with", "you", "your",
})

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


def _security_canonical(text):
    """Canonicalize security-significant text before resource matching."""
    value = unicodedata.normalize("NFKC", _clean(text))
    value = value.replace("C++", "cpp").replace("c++", "cpp")
    value = value.replace("C#", "csharp").replace("c#", "csharp")
    value = value.translate(str.maketrans({
        "А": "A", "а": "a", "С": "C", "с": "c", "І": "I", "і": "i",
        "Е": "E", "е": "e", "О": "O", "о": "o", "Р": "P", "р": "p",
        "Х": "X", "х": "x",
    })).casefold()
    value = "".join(
        char for char in value if unicodedata.category(char) != "Cf"
    )
    value = "".join(
        " " if unicodedata.category(char)[:1] in {"P", "S", "Z"} else char
        for char in value
    )
    return _SECURITY_SEPARATOR_RE.sub(" ", value).strip()


def _has_sensitive_resource(text):
    """Detect prompt-bearing resources, except narrow syntax-only discussion."""
    value = _security_canonical(text)
    if _BENIGN_RESOURCE_CANONICAL_RE.fullmatch(value):
        return False
    return bool(_SENSITIVE_RESOURCE_CANONICAL_RE.search(value))


def _category_shape_safe(category, text):
    """Require a complete known-safe grammar, not keyword classification."""
    value = _security_canonical(text)
    patterns = {
        "shell": _SAFE_SHELL_CANONICAL_RE,
        "code": _SAFE_CODE_CANONICAL_RE,
        "ui": _SAFE_UI_CANONICAL_RE,
        "workflow": _SAFE_WORKFLOW_CANONICAL_RE,
        "response_style": _SAFE_STYLE_CANONICAL_RE,
    }
    pattern = patterns.get(category)
    return bool(pattern and pattern.fullmatch(value))


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
        or _SENSITIVE_DISCLOSURE_RE.search(value)
        or _has_sensitive_resource(value)
        or any(ord(char) < 32 for char in value)
    ):
        return ""
    if _BENIGN_RESOURCE_CANONICAL_RE.fullmatch(_security_canonical(value)):
        return "shell"
    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.search(value):
            if category == "identity" or _category_shape_safe(category, value):
                return category
    if (
        _GENERIC_DURABLE_RE.search(value)
        and _SAFE_GENERAL_DEFAULT_RE.fullmatch(value)
    ):
        return "general"
    return ""


def _topical_terms(text):
    """Return bounded subject terms for a durable topical directive."""
    match = _TOPICAL_DIRECTIVE_RE.search(_clean(text))
    if not match:
        return frozenset()
    return frozenset(
        word.casefold()
        for word in _TOPIC_WORD_RE.findall(match.group("subject"))
        if len(word) > 2 and word.casefold() not in _TOPIC_STOP_WORDS
    )


def _task_terms(text):
    return frozenset(
        word.casefold()
        for word in _TOPIC_WORD_RE.findall(_clean(text))
        if len(word) > 2 and word.casefold() not in _TOPIC_STOP_WORDS
    )


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
        or _SENSITIVE_DISCLOSURE_RE.search(source)
        or _has_sensitive_resource(source)
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
    if category in {"general", "identity", "response_style"}:
        return True
    task = str(task or "")
    if category == "topic":
        return bool(_topical_terms(text) & _task_terms(task))
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
        or _SENSITIVE_DISCLOSURE_RE.search(text)
        or _has_sensitive_resource(text)
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
