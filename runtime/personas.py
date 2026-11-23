"""personas — swappable system-prompt presets and auto-routing for Sonder Runtime.

Lets Sonder Runtime serve specialist modes (coder, reviewer, security, architect,
creative, concise, explainer, teacher, mathematician) — either manually pinned
or auto-routed per turn from the active tier and prompt cues. The text of each
persona is an editable Markdown prompt (``prompts/personas/<name>.md``,
overridable per operator; see ``sonder_runtime.adapters.prompt_store``), read
fresh each turn.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import re

from sonder_runtime.adapters import prompt_store as _prompts

DEFAULT = "coder"
AUTO = "auto"
OFF = "off"
MODES = (AUTO, OFF)

_NAMES = tuple(sorted(
    name.split("/", 1)[1] for name in _prompts.CATALOG if name.startswith("personas/")
))

_ALIASES = {
    "auto": AUTO,
    "default": AUTO,
    "reset": AUTO,
    "off": OFF,
    "none": OFF,
    "disabled": OFF,
    "arch": "architect",
    "brief": "concise",
    "short": "concise",
    "terse": "concise",
    "explain": "explainer",
    "math": "mathematician",
    "review": "reviewer",
    "sec": "security",
    "teach": "teacher",
}

_TIER_PERSONAS = {
    "security": "security",
    "creative": "creative",
    "draft": "concise",
}

_PROMPT_PATTERNS = (
    (
        "mathematician",
        re.compile(
            r"\b(?:lean\s*4|formal\s+proof|prove\s+(?:that|the|this)\b|"
            r"theorem|lemma|counterexample|q\.?e\.?d\.?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "security",
        re.compile(
            r"\b(?:threat\s+model|vulnerabilit(?:y|ies)|cve-\d+|owasp|"
            r"sql\s*injection|xss|csrf|ssrf|privilege\s+escalation|"
            r"security\s+(?:audit|review|hardening)|harden\s+(?:this|the|my))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "reviewer",
        re.compile(
            r"\b(?:code\s+review|"
            r"review\s+(?:this|my|the)\s+(?:code|diff|pr|pull\s+request|patch|function|class|file)|"
            r"critique\s+(?:this|my)\s+code|find\s+bugs?\s+in)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "architect",
        re.compile(
            r"\b(?:system\s+design|"
            r"architecture\s+(?:design|review|plan|proposal)|"
            r"design\s+(?:an?\s+)?architecture|"
            r"trade[\s-]?offs?\s+between|write\s+(?:an?\s+)?rfc|"
            r"high[\s-]?level\s+design)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "creative",
        re.compile(
            r"\b(?:write\s+(?:me\s+)?(?:a\s+)?(?:short\s+)?"
            r"(?:story|poem|sonnet|haiku|dialogue|scene|screenplay|monologue|lore|fable)|"
            r"worldbuilding|creative\s+writing|in\s+the\s+style\s+of)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "teacher",
        re.compile(
            r"\b(?:teach\s+me|step[\s-]by[\s-]step\s+(?:lesson|tutorial|guide)|"
            r"quiz\s+me|walk\s+me\s+through\s+learning)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "explainer",
        re.compile(
            r"\b(?:eli5|explain\s+like\s+i(?:'|\u2019)?m|"
            r"plain\s+(?:english|language)|in\s+simple\s+terms|"
            r"for\s+a\s+non[\s-]?expert)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "concise",
        re.compile(
            r"\b(?:tl;?dr|be\s+brief|one[\s-]?liner|short\s+answer\s+only|"
            r"answer\s+concisely|briefly\s+summarize)\b",
            re.IGNORECASE,
        ),
    ),
)

_ACTIVE_PERSONA: ContextVar[str] = ContextVar("sonder_active_persona", default="")


def get(name):
    """Return the system prompt for `name`, falling back to DEFAULT.

    Case/whitespace-insensitive; None or unknown names fall back to coder.
    """
    key = (name or DEFAULT).strip().lower()
    if key not in _NAMES:
        key = DEFAULT
    return _prompts.render("personas/" + key)


def names():
    """Return the sorted list of available persona names."""
    return list(_NAMES)


def descriptions():
    """Return a dict mapping each persona name to its catalog summary."""
    return {
        name: _prompts.CATALOG["personas/" + name].summary
        for name in _NAMES
    }


def normalize_selection(raw):
    """Normalize a user/API persona selector to `'auto'`, `'off'`, or a persona name.

    Returns ``None`` for unrecognized or non-string values. Empty/None maps to ``'auto'``.
    """
    if raw is None:
        return AUTO
    if not isinstance(raw, str):
        return None
    key = raw.strip().lower()
    if not key:
        return AUTO
    mapped = _ALIASES.get(key, key)
    if mapped in MODES or mapped in _NAMES:
        return mapped
    return None


def resolve_auto(prompt="", *, tier=""):
    """Pick the best specialist persona for a turn, or ``""`` for plain default chat."""
    tier_key = (tier or "").strip().lower()
    if tier_key in _TIER_PERSONAS:
        candidate = _TIER_PERSONAS[tier_key]
        if candidate in _NAMES:
            return candidate
    text = (prompt or "").strip()
    if text:
        for persona_name, pattern in _PROMPT_PATTERNS:
            if persona_name in _NAMES and pattern.search(text):
                return persona_name
    return ""


def resolve(selected, *, prompt="", tier=""):
    """Resolve a persona setting (``""``, ``"auto"``, ``"off"``, or a name) to a concrete persona name or ``""``."""
    if not selected or not isinstance(selected, str):
        return ""
    key = selected.strip().lower()
    if not key:
        return ""
    mapped = _ALIASES.get(key, key)
    if mapped == OFF:
        return ""
    if mapped == AUTO:
        return resolve_auto(prompt=prompt, tier=tier)
    if mapped in _NAMES:
        return mapped
    return DEFAULT


def active_scope_persona():
    """Return the persona selector bound to the current turn scope, or ``""``."""
    return _ACTIVE_PERSONA.get()


@contextmanager
def persona_scope(value):
    """Bind a persona selector (``"auto"``, ``"off"``, or a persona name) for this turn."""
    normalized = normalize_selection(value) if value else ""
    token = _ACTIVE_PERSONA.set(normalized or "")
    try:
        yield normalized or ""
    finally:
        _ACTIVE_PERSONA.reset(token)


def __getattr__(attribute):
    # ``PERSONAS`` used to be a literal dict; keep the name for callers that
    # read it, but make it a fresh snapshot of the editable prompts.
    if attribute == "PERSONAS":
        return {name: get(name) for name in _NAMES}
    raise AttributeError("module %r has no attribute %r" % (__name__, attribute))
